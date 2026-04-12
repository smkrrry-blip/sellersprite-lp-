"""
Shopee OAuth認証ヘルパー
アクセストークンを取得・更新するためのスクリプト
"""
import hmac
import time
import hashlib
import webbrowser
import http.server
import urllib.parse
import logging
from config import SHOPEE_PARTNER_ID, SHOPEE_PARTNER_KEY, SHOPEE_ENV, SHOPEE_BASE_URL

logger = logging.getLogger(__name__)
BASE_URL = SHOPEE_BASE_URL.get(SHOPEE_ENV, SHOPEE_BASE_URL["production"])
REDIRECT_URI = "http://localhost:8080/callback"


def sign_auth(path: str, timestamp: int) -> str:
    """認証用署名（shop_idなし）"""
    base = f"{SHOPEE_PARTNER_ID}{path}{timestamp}"
    return hmac.new(
        SHOPEE_PARTNER_KEY.encode(),
        base.encode(),
        hashlib.sha256
    ).hexdigest()


def get_auth_url() -> str:
    """ショップ認証URLを生成"""
    path = "/api/v2/shop/auth_partner"
    timestamp = int(time.time())
    sign = sign_auth(path, timestamp)
    url = (
        f"{BASE_URL}{path}"
        f"?partner_id={SHOPEE_PARTNER_ID}"
        f"&timestamp={timestamp}"
        f"&sign={sign}"
        f"&redirect={REDIRECT_URI}"
    )
    return url


def get_access_token(code: str, shop_id: int) -> dict:
    """認証コードからアクセストークンを取得"""
    import requests
    path = "/api/v2/auth/token/get"
    timestamp = int(time.time())
    sign = sign_auth(path, timestamp)
    url = BASE_URL + path
    params = {
        "partner_id": SHOPEE_PARTNER_ID,
        "timestamp": timestamp,
        "sign": sign,
    }
    body = {
        "code": code,
        "shop_id": shop_id,
        "partner_id": int(SHOPEE_PARTNER_ID),
    }
    resp = requests.post(url, params=params, json=body, timeout=15)
    return resp.json()


def refresh_access_token(refresh_token: str, shop_id: int) -> dict:
    """アクセストークンを更新"""
    import requests
    path = "/api/v2/auth/access_token/get"
    timestamp = int(time.time())
    sign = sign_auth(path, timestamp)
    url = BASE_URL + path
    params = {
        "partner_id": SHOPEE_PARTNER_ID,
        "timestamp": timestamp,
        "sign": sign,
    }
    body = {
        "refresh_token": refresh_token,
        "shop_id": shop_id,
        "partner_id": int(SHOPEE_PARTNER_ID),
    }
    resp = requests.post(url, params=params, json=body, timeout=15)
    return resp.json()


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """OAuthコールバックを受け取るローカルサーバー"""
    received_code = None
    received_shop_id = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        CallbackHandler.received_code = params.get("code", [None])[0]
        CallbackHandler.received_shop_id = params.get("shop_id", [None])[0]

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"""
        <html><body style="font-family:sans-serif;padding:40px">
        <h2>&#x2705; Shopee認証完了！</h2>
        <p>このウィンドウを閉じてターミナルに戻ってください。</p>
        </body></html>
        """)

    def log_message(self, format, *args):
        pass


def interactive_auth():
    """
    ブラウザでShopee認証を実行し、アクセストークンを取得する
    使い方: python auth_shopee.py
    """
    if not SHOPEE_PARTNER_ID or not SHOPEE_PARTNER_KEY:
        print("❌ config.py に SHOPEE_PARTNER_ID と SHOPEE_PARTNER_KEY を設定してください")
        return

    auth_url = get_auth_url()
    print(f"\n🔐 Shopee認証を開始します")
    print(f"ブラウザが開きます。Shopeeアカウントでログインして承認してください。")
    print(f"\n認証URL: {auth_url}\n")
    webbrowser.open(auth_url)

    print("ローカルサーバーを起動中 (port 8080)...")
    server = http.server.HTTPServer(("localhost", 8080), CallbackHandler)
    server.handle_request()

    code = CallbackHandler.received_code
    shop_id_str = CallbackHandler.received_shop_id

    if not code or not shop_id_str:
        print("❌ 認証コードを受け取れませんでした")
        return

    print(f"\n受信: code={code[:20]}..., shop_id={shop_id_str}")

    shop_id = int(shop_id_str)
    result = get_access_token(code, shop_id)

    if result.get("access_token"):
        print("\n✅ アクセストークン取得成功！")
        print("\nconfig.py に以下を設定してください:\n")
        print(f'SHOPEE_SHOP_ID      = "{shop_id}"')
        print(f'SHOPEE_ACCESS_TOKEN = "{result["access_token"]}"')
        print(f'\n# リフレッシュトークン（30日後に更新用）:')
        print(f'# SHOPEE_REFRESH_TOKEN = "{result.get("refresh_token", "")}"')
        print(f'\n# トークン有効期限: {result.get("expire_in", 0) // 3600} 時間')
    else:
        print(f"❌ トークン取得失敗: {result}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    interactive_auth()
