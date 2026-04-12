"""
ChromeのShopee Cookieを抽出してPlaywright用 cookies.json に変換する
実行前にChromeを完全に閉じてください（DBロック回避のため）
"""
import json
import sqlite3
import shutil
import tempfile
import os
import sys
from pathlib import Path
from datetime import datetime

COOKIES_FILE = Path(__file__).parent / "cookies.json"

# ChromeのCookieDBパス（macOS）
CHROME_COOKIE_PATH = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
# Google Chrome for Testing（Playwright起動時に使われる場合あり）
CHROME_FOR_TESTING_PATH = Path.home() / "Library/Application Support/Google/Chrome for Testing/Default/Cookies"

TARGET_DOMAINS = [
    ".shopee.co.th",
    "shopee.co.th",
    ".accounts.shopee.co.th",
    "accounts.shopee.co.th",
    ".seller.shopee.co.th",
    "seller.shopee.co.th",
]


def find_chrome_cookie_db() -> Path:
    """ChromeのCookie DBファイルを探す"""
    candidates = [CHROME_COOKIE_PATH, CHROME_FOR_TESTING_PATH]
    for p in candidates:
        if p.exists():
            print(f"Cookie DB 発見: {p}")
            return p
    raise FileNotFoundError(
        "ChromeのCookie DBが見つかりません。\n"
        "通常の Chrome がインストールされているか確認してください。\n"
        f"探した場所:\n" + "\n".join(f"  {p}" for p in candidates)
    )


def extract_cookies_sqlite(db_path: Path) -> list[dict]:
    """
    SQLiteから直接Cookieを読み込む
    ※ Chrome起動中は失敗する場合があります（その場合はChromeを閉じてください）
    """
    # DBをコピーしてロック回避
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        cursor = conn.cursor()

        # Cookieテーブルのカラム名を確認
        cursor.execute("PRAGMA table_info(cookies)")
        columns = [row[1] for row in cursor.fetchall()]

        # ドメインフィルタで取得
        placeholders = ",".join("?" * len(TARGET_DOMAINS))
        cursor.execute(
            f"SELECT * FROM cookies WHERE host_key IN ({placeholders})",
            TARGET_DOMAINS,
        )
        rows = cursor.fetchall()
        conn.close()

        cookies = []
        for row in rows:
            row_dict = dict(zip(columns, row))
            # encrypted_value は復号不可のためスキップ、value のみ使用
            value = row_dict.get("value", "")
            if not value and row_dict.get("encrypted_value"):
                # 暗号化されている場合はスキップ（後述の browser_cookie3 を使う）
                continue

            cookies.append({
                "name": row_dict.get("name", ""),
                "value": value,
                "domain": row_dict.get("host_key", ""),
                "path": row_dict.get("path", "/"),
                "expires": row_dict.get("expires_utc", -1),
                "httpOnly": bool(row_dict.get("is_httponly", 0)),
                "secure": bool(row_dict.get("is_secure", 0)),
                "sameSite": "Lax",
            })

        return cookies

    finally:
        os.unlink(tmp_path)


def extract_cookies_browser_cookie3() -> list[dict]:
    """
    browser_cookie3 ライブラリを使ってCookieを取得（暗号化対応）
    pip install browser-cookie3 が必要
    """
    try:
        import browser_cookie3
    except ImportError:
        print("browser-cookie3 が未インストールです。インストールします...")
        os.system(f"{sys.executable} -m pip install browser-cookie3 --quiet")
        import browser_cookie3

    cookies = []
    for loader, name in [
        (browser_cookie3.chrome, "Chrome"),
        (browser_cookie3.chromium, "Chromium"),
    ]:
        try:
            cj = loader(domain_name=".shopee.co.th")
            for cookie in cj:
                if any(d in cookie.domain for d in ["shopee.co.th"]):
                    cookies.append({
                        "name": cookie.name,
                        "value": cookie.value,
                        "domain": cookie.domain,
                        "path": cookie.path or "/",
                        "expires": int(cookie.expires) if cookie.expires else -1,
                        "httpOnly": False,
                        "secure": bool(cookie.secure),
                        "sameSite": "Lax",
                    })
            if cookies:
                print(f"✅ {name} から {len(cookies)} 件取得")
                break
        except Exception as e:
            print(f"  {name}: {e}")

    return cookies


def save_playwright_cookies(cookies: list[dict]):
    """Playwright の storage_state 形式で保存"""
    storage_state = {
        "cookies": cookies,
        "origins": [],
    }
    COOKIES_FILE.write_text(json.dumps(storage_state, ensure_ascii=False, indent=2))
    print(f"\n✅ {len(cookies)} 件のCookieを保存しました: {COOKIES_FILE}")


def main():
    print("=" * 50)
    print("  Shopee Cookie エクスポーター")
    print("=" * 50)
    print(f"対象ドメイン: {TARGET_DOMAINS}")
    print()

    cookies = []

    # まず browser_cookie3 で試みる（暗号化Cookie対応）
    print("【方法1】browser_cookie3 で取得中...")
    try:
        cookies = extract_cookies_browser_cookie3()
    except Exception as e:
        print(f"  失敗: {e}")

    # フォールバック: SQLite直接読み込み
    if not cookies:
        print("\n【方法2】SQLite直接読み込みで取得中...")
        try:
            db_path = find_chrome_cookie_db()
            cookies = extract_cookies_sqlite(db_path)
            if cookies:
                print(f"  {len(cookies)} 件取得（非暗号化のみ）")
        except Exception as e:
            print(f"  失敗: {e}")

    if not cookies:
        print("\n❌ Cookieの取得に失敗しました。")
        print("   → Chromeを完全に閉じてから再実行してください")
        print("   → またはChromeでShopeeにログインし直してください")
        sys.exit(1)

    save_playwright_cookies(cookies)
    print("\n次のステップ:")
    print("  python3 shopee_browser.py")
    print("  （ログインをスキップしてセラーセンターに直接アクセスします）")


if __name__ == "__main__":
    main()
