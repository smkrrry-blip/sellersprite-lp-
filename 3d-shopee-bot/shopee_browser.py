"""
Shopee セラーセンター Playwright ブラウザ自動化
seller.shopee.co.th を実際のChrome操作で出品する
"""
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout

CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
MAX_CONSECUTIVE_SESSION_ERRORS = 3  # 連続セッション切れでループ検出→中断
MAX_CONSECUTIVE_NAV_ERRORS = 3  # 連続ナビゲーション失敗でループ検出→中断


class LoginLoopError(Exception):
    """ログイン→セッション切れのループを検出した場合に raise"""
    pass


class NavigationLoopError(Exception):
    """product/new への連続ナビゲーション失敗（ERR_ABORTED等）を検出した場合に raise"""
    pass

from config import (
    SHOPEE_EMAIL, SHOPEE_PASSWORD, SHOPEE_SELLER_URL,
    BROWSER_SETTINGS, LISTING_SETTINGS, CATEGORY_MAP,
)
from db import update_status

logger = logging.getLogger(__name__)

BOT_DIR = Path(__file__).parent
COOKIES_FILE = BOT_DIR / "cookies.json"
ERRORS_DIR = BOT_DIR / "errors"
ERRORS_DIR.mkdir(exist_ok=True)

# 1日の出品カウンタファイル
DAILY_COUNT_FILE = BOT_DIR / "data" / "daily_count.json"


def _human_wait(min_sec: float = None, max_sec: float = None):
    """人間らしいランダム待機"""
    lo = min_sec if min_sec is not None else BROWSER_SETTINGS["min_wait"]
    hi = max_sec if max_sec is not None else BROWSER_SETTINGS["max_wait"]
    time.sleep(random.uniform(lo, hi))


def _human_type(page: Page, selector: str, text: str):
    """人間らしいタイピング（1文字ずつランダム速度）"""
    element = page.locator(selector).first
    element.click()
    _human_wait(0.3, 0.7)
    for char in text:
        page.keyboard.type(char)
        time.sleep(random.uniform(0.03, 0.12))


def _random_scroll(page: Page):
    """ランダムなスクロールで人間らしさを演出"""
    scroll_amount = random.randint(100, 400)
    page.mouse.wheel(0, scroll_amount)
    _human_wait(0.5, 1.5)
    page.mouse.wheel(0, -scroll_amount // 2)
    _human_wait(0.3, 0.8)


def _get_today_count() -> int:
    """本日の出品数を取得"""
    DAILY_COUNT_FILE.parent.mkdir(exist_ok=True)
    if not DAILY_COUNT_FILE.exists():
        return 0
    try:
        data = json.loads(DAILY_COUNT_FILE.read_text())
        if data.get("date") == str(date.today()):
            return data.get("count", 0)
    except Exception:
        pass
    return 0


def _increment_today_count():
    """本日の出品数をインクリメント"""
    count = _get_today_count() + 1
    DAILY_COUNT_FILE.write_text(json.dumps({"date": str(date.today()), "count": count}))
    return count


def _notify_captcha():
    """CAPTCHAが出た場合にmacOSデスクトップ通知"""
    try:
        os.system(
            'osascript -e \'display notification "CAPTCHAが検出されました。手動で解決してください。" '
            'with title "⚠️ Shopee Bot 停止" subtitle "seller.shopee.co.th" sound name "Sosumi"\''
        )
    except Exception:
        pass


class ShopeeBrowser:
    """Playwright を使って Shopee セラーセンターを操作するクラス"""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._using_cdp = False
        self._session_error_count = 0
        self._nav_error_count = 0
        self._auto_chrome_proc: Optional[subprocess.Popen] = None

    # ─── 起動・終了 ───────────────────────────────────────

    def _launch_chrome_debug(self) -> None:
        """Chrome をデバッグモードで自動起動する"""
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        profile_dir = os.path.expanduser("~/.shopee-bot-chrome-profile")
        os.makedirs(profile_dir, exist_ok=True)
        try:
            self._auto_chrome_proc = subprocess.Popen(
                [
                    chrome_path,
                    f"--remote-debugging-port={CDP_PORT}",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"{SHOPEE_SELLER_URL}/account/login",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"🚀 Chrome 自動起動 (PID {self._auto_chrome_proc.pid}, port {CDP_PORT})")
        except Exception as e:
            logger.error(f"Chrome 起動失敗: {e}")

    def start(self):
        """ブラウザを起動してコンテキストを初期化。CDP接続を優先する。"""
        import urllib.request
        self._playwright = sync_playwright().start()

        # ── CDP接続を試みる（既存Chromeセッション優先）──────────────
        cdp_ready = False
        try:
            urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2)
            cdp_ready = True
        except Exception:
            pass

        # CDP未起動なら Chrome を自動起動して待つ（最大15秒）
        if not cdp_ready:
            logger.info(f"CDP未起動 — Chrome を自動起動します (port {CDP_PORT})")
            self._launch_chrome_debug()
            for _ in range(30):
                time.sleep(0.5)
                try:
                    urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1)
                    cdp_ready = True
                    break
                except Exception:
                    pass
            if not cdp_ready:
                logger.warning("Chrome CDP 起動タイムアウト — 通常起動にフォールバック")

        if cdp_ready:
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(CDP_URL)
                self._context = self._browser.contexts[0]
                self._page = self._context.new_page()
                self._using_cdp = True
                logger.info(f"✅ CDP接続成功 (port {CDP_PORT})")
                return
            except Exception as e:
                logger.warning(f"CDP接続失敗 ({e}) — 通常起動にフォールバック")

        # ── 通常のPlaywright起動（フォールバック）──────────────────
        self._browser = self._playwright.chromium.launch(
            headless=BROWSER_SETTINGS["headless"],
            slow_mo=BROWSER_SETTINGS["slow_mo"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
        )
        storage_state = str(COOKIES_FILE) if COOKIES_FILE.exists() else None
        self._context = self._browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="th-TH",
            timezone_id="Asia/Bangkok",
        )
        self._page = self._context.new_page()
        self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        logger.info("✅ ブラウザ起動完了")

    def stop(self):
        """ブラウザを終了。自動起動した Chrome は終了、手動起動はそのまま。"""
        try:
            if self._using_cdp:
                if self._page and not self._page.is_closed():
                    self._page.close()
                # 自動起動した Chrome のみ終了する（手動起動のChromeは維持）
                if self._auto_chrome_proc is not None:
                    try:
                        self._auto_chrome_proc.terminate()
                        logger.info("Chrome 自動起動プロセスを終了しました")
                    except Exception:
                        pass
            else:
                if self._context:
                    self._context.close()
                if self._browser:
                    self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.warning(f"ブラウザ終了エラー: {e}")
        logger.info("ブラウザ終了")

    def _save_cookies(self):
        """現在のセッションCookieをファイルに保存"""
        try:
            storage = self._context.storage_state()
            COOKIES_FILE.write_text(json.dumps(storage, ensure_ascii=False, indent=2))
            logger.info(f"Cookie保存: {COOKIES_FILE}")
        except Exception as e:
            logger.warning(f"Cookie保存失敗: {e}")

    # ─── ログイン ──────────────────────────────────────────

    def _is_logged_in(self) -> bool:
        """ログイン済みかどうか確認"""
        try:
            self._page.goto(f"{SHOPEE_SELLER_URL}/portal/product/list/all", timeout=15000)
            _human_wait(2, 4)
            # ログインページにリダイレクトされていないか確認
            return "login" not in self._page.url and "portal/product" in self._page.url
        except Exception:
            return False

    def login(self) -> bool:
        """
        Shopeeセラーセンターにログイン
        Cookieが有効ならスキップ、無効なら再ログイン
        """
        # CDP接続時はCookieファイルに関わらず既存セッションを先に確認
        if self._using_cdp or COOKIES_FILE.exists():
            logger.info("セッション確認中...")
            if self._is_logged_in():
                logger.info("✅ セッション有効 — ログインスキップ")
                self._save_cookies()
                return True
            logger.info("セッション無効 — 再ログインします")

        logger.info("🔑 ログイン開始...")
        try:
            self._page.goto(f"{SHOPEE_SELLER_URL}/account/login", timeout=30000)

            # ページ完全読み込みを待つ
            self._page.wait_for_load_state("networkidle", timeout=15000)
            _human_wait(2, 3)

            # 言語選択モーダルが出るのを待ってから閉じる
            try:
                self._page.wait_for_selector(
                    'button:has-text("English"), button:has-text("ไทย")',
                    timeout=8000,
                )
                logger.info("言語選択モーダル検出")
            except Exception:
                logger.info("言語選択モーダルなし（スキップ）")

            # モーダル・ポップアップを閉じる
            self._dismiss_modals()
            _human_wait(1, 2)

            # CAPTCHA検出
            if self._detect_captcha():
                logger.error("❌ ログインページにCAPTCHAが表示されています")
                _notify_captcha()
                return False

            # メールアドレス入力（fill() で直接入力 — モーダルのクリックブロック回避）
            input_sel = 'input[name="loginKey"], input[type="text"]'
            self._page.wait_for_selector(input_sel, timeout=10000)
            _human_wait(0.5, 1.0)
            self._page.locator(input_sel).first.fill(SHOPEE_EMAIL)
            _human_wait(0.5, 1.0)

            # パスワード入力
            self._page.locator('input[type="password"]').first.fill(SHOPEE_PASSWORD)
            _human_wait(0.8, 1.5)

            # ログインボタンクリック（複数セレクターで確実に）
            login_btn_sel = (
                'button:has-text("LOG IN"), '
                'button:has-text("Log In"), '
                'button:has-text("เข้าสู่ระบบ"), '
                'button[type="submit"]'
            )
            self._page.wait_for_selector(login_btn_sel, timeout=8000)
            _human_wait(0.5, 1.0)
            self._page.locator(login_btn_sel).first.click()
            logger.info("LOG IN ボタンをクリックしました")
            _human_wait(4, 7)

            # CAPTCHA検出（ログイン後）
            if self._detect_captcha():
                logger.error("❌ ログイン後にCAPTCHAが表示されています")
                _notify_captcha()
                return False

            # ログイン成功確認
            if "login" not in self._page.url:
                self._save_cookies()
                logger.info("✅ ログイン成功")
                return True
            else:
                logger.error("❌ ログイン失敗 — URLがログインページのまま")
                self._screenshot("login_failed")
                return False

        except PlaywrightTimeout as e:
            logger.error(f"❌ ログインタイムアウト: {e}")
            self._screenshot("login_timeout")
            return False
        except Exception as e:
            logger.error(f"❌ ログインエラー: {e}")
            self._screenshot("login_error")
            return False

    def _dismiss_modals(self):
        """
        ログイン前に表示されるモーダル・ポップアップを閉じる
        （言語選択、Cookie同意、通知許可、プロモーションバナー等）
        """
        # ── 言語選択モーダル（最優先） ──────────────────────
        # 「เลือกภาษา」ポップアップの「English」または「ไทย」を選択
        lang_selectors = [
            'button:has-text("English")',
            'button:has-text("ไทย")',
        ]
        for sel in lang_selectors:
            try:
                btn = self._page.locator(sel).first
                if btn.count() and btn.is_visible():
                    btn.click()
                    logger.info(f"言語選択モーダルを閉じました: {sel}")
                    _human_wait(1.0, 2.0)
                    break
            except Exception:
                pass

        # 言語モーダルが完全に消えるまで待つ
        try:
            self._page.wait_for_selector(
                'button:has-text("English"), button:has-text("ไทย")',
                state="hidden",
                timeout=5000,
            )
        except Exception:
            pass

        # ESCキーで残りのモーダルを閉じる（ソーシャルログインボタンには触れない）
        try:
            self._page.keyboard.press("Escape")
            _human_wait(0.5, 1.0)
        except Exception:
            pass

    def _detect_captcha(self) -> bool:
        """CAPTCHAの存在を検出"""
        captcha_selectors = [
            ".captcha",
            "#captcha",
            'iframe[src*="captcha"]',
            'iframe[src*="recaptcha"]',
            ".shopee-captcha",
            '[class*="captcha"]',
        ]
        for sel in captcha_selectors:
            try:
                if self._page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False

    # ─── 画像アップロード ──────────────────────────────────

    @staticmethod
    def _prepare_images(paths: list[str]) -> list[str]:
        """
        Shopee要件（正方形 500x500以上）に合わせて画像をリサイズする。
        元ファイルは変更せず、tmpディレクトリにコピーして返す。
        """
        try:
            from PIL import Image as PILImage
        except ImportError:
            return paths  # PIL未インストールはそのまま

        MIN_SIZE = 500
        prepared = []
        tmpdir = tempfile.mkdtemp(prefix="shopee_img_")
        for p in paths:
            try:
                img = PILImage.open(p).convert("RGB")
                w, h = img.size
                # 正方形にクロップ（中央）
                if w != h:
                    s = min(w, h)
                    left = (w - s) // 2
                    top = (h - s) // 2
                    img = img.crop((left, top, left + s, top + s))
                # 500x500 未満はスケールアップ
                if img.size[0] < MIN_SIZE:
                    img = img.resize((MIN_SIZE, MIN_SIZE), PILImage.LANCZOS)
                out_path = os.path.join(tmpdir, os.path.basename(p))
                img.save(out_path, "JPEG", quality=90)
                prepared.append(out_path)
            except Exception as e:
                logger.warning(f"  画像前処理スキップ {p}: {e}")
                prepared.append(p)  # 元ファイルそのまま使用
        return prepared

    def _upload_images(self, local_image_paths: list[str]) -> bool:
        """ローカル画像ファイルをShopeeフォームにアップロード"""
        if not local_image_paths:
            logger.warning("アップロードする画像がありません")
            return False

        try:
            # ファイル入力要素を探す
            file_input = self._page.locator('input[type="file"]').first
            if not file_input:
                logger.error("ファイル入力要素が見つかりません")
                return False

            # 存在するファイルのみ渡す
            valid_paths = [p for p in local_image_paths if Path(p).exists()]
            if not valid_paths:
                logger.error("有効な画像ファイルがありません")
                return False

            # Shopee要件: 正方形 500x500以上 にリサイズ
            valid_paths = self._prepare_images(valid_paths)

            file_input.set_input_files(valid_paths[:9])  # Shopee最大9枚
            _human_wait(2, 4)  # アップロード完了待ち

            # ── アップロード後のエラーポップアップを閉じる ──────────────────
            # "Your product image may not be of acceptable resolution" など
            # "Notice" ダイアログの "Confirm" ボタンが出たら自動クローズ
            try:
                confirm_btn = self._page.locator(
                    'button:has-text("Confirm"), button:has-text("OK"), button:has-text("ยืนยัน")'
                ).first
                if confirm_btn.count() and confirm_btn.is_visible():
                    logger.info("  画像アップロード Notice → Confirm クリック")
                    confirm_btn.click()
                    _human_wait(1, 1.5)
            except Exception:
                pass

            logger.info(f"✅ 画像アップロード: {len(valid_paths)} 枚")
            return True

        except Exception as e:
            logger.error(f"❌ 画像アップロードエラー: {e}")
            return False

    # ─── 商品出品 ──────────────────────────────────────────

    def list_product(self, product: dict, local_image_paths: list[str]) -> Optional[str]:
        """
        Shopeeセラーセンターで商品を出品する
        product: 翻訳済み商品データ（title_th, description_th, price_thb等）
        local_image_paths: ローカルに保存済みの画像ファイルパスリスト
        → 成功時: 出品ページURL / 失敗時: None
        """
        # 1日の出品上限チェック
        today_count = _get_today_count()
        if today_count >= BROWSER_SETTINGS["daily_limit"]:
            logger.warning(f"⚠️ 1日の出品上限 ({BROWSER_SETTINGS['daily_limit']}件) に達しました")
            return None

        mw_id = product.get("mw_model_id", "unknown")

        try:
            # セラーセンターに移動してから「Add New Product」をクリック
            logger.info(f"📦 出品開始: {product.get('title_th', '')[:40]}")

            # 商品リストページに移動してドラフト状態をクリアしてから新規出品へ
            # wait_until="domcontentloaded" で高速化（全リソース読み込み待ち不要）
            try:
                self._page.goto(
                    f"{SHOPEE_SELLER_URL}/portal/product/list/all",
                    timeout=45000,
                    wait_until="domcontentloaded",
                )
                _human_wait(1, 2)
            except Exception as e:
                logger.warning(f"  product/list/all ナビゲーション失敗（続行）: {e}")

            # 出品ページへ直接移動（ERR_ABORTED 等の連続失敗を検出してループ中断）
            try:
                self._page.goto(
                    f"{SHOPEE_SELLER_URL}/portal/product/new",
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
                self._nav_error_count = 0  # 成功時はカウンタリセット
            except Exception as _nav_e:
                self._nav_error_count += 1
                logger.error(
                    f"❌ /product/new ナビゲーション失敗 "
                    f"({self._nav_error_count}/{MAX_CONSECUTIVE_NAV_ERRORS}): {_nav_e}"
                )
                if self._nav_error_count >= MAX_CONSECUTIVE_NAV_ERRORS:
                    raise NavigationLoopError(
                        f"/product/new への連続失敗が{self._nav_error_count}回 — 自動中断"
                    )
                raise
            _human_wait(2, 3)

            # /product/new がログインにリダイレクトされた場合: リロード前に再ログイン
            if "login" in self._page.url:
                logger.info("セッション切れ (/product/new) — 再ログイン")
                if not self.login():
                    self._session_error_count += 1
                    if self._session_error_count >= MAX_CONSECUTIVE_SESSION_ERRORS:
                        raise LoginLoopError(
                            f"ログイン失敗が{self._session_error_count}回連続 — 自動中断"
                        )
                    return None
                self._page.goto(
                    f"{SHOPEE_SELLER_URL}/portal/product/new",
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
                _human_wait(3, 5)
                try:
                    self._page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                if "login" in self._page.url:
                    self._session_error_count += 1
                    logger.error(
                        f"❌ 再ログイン後も /product/new でセッション切れ "
                        f"({self._session_error_count}/{MAX_CONSECUTIVE_SESSION_ERRORS})"
                    )
                    if self._session_error_count >= MAX_CONSECUTIVE_SESSION_ERRORS:
                        raise LoginLoopError(
                            f"ログイン→セッション切れのループを{self._session_error_count}回検出 — 自動中断"
                        )
                    return None
                # ログイン成功・セッション回復
                self._session_error_count = 0

            # ページをリロードしてドラフト復元をクリア
            self._page.reload(wait_until="domcontentloaded", timeout=60000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            _human_wait(3, 5)

            logger.info(f"  URL: {self._page.url}")

            # sspSearchTour のみ削除（他のオーバーレイは削除しない）
            self._page.evaluate("document.getElementById('sspSearchTour')?.remove()")
            _human_wait(0.5, 1.0)

            if self._detect_captcha():
                logger.error("❌ 出品ページでCAPTCHA検出")
                _notify_captcha()
                return None

            # リロード後も login リダイレクトになった場合
            if "login" in self._page.url:
                logger.error("❌ リロード後もログインページ — スキップ")
                return None

            # ════════════════════════════════════════
            # STEP 1: 商品名 + 画像
            # ════════════════════════════════════════
            title_raw = str(product.get("title_th") or product.get("title_en") or "3D Print Item")
            # Shopeeは最低25文字必要 — 短い場合はサフィックスを追加
            if len(title_raw) < 25:
                suffix = " - สินค้า 3D Printed คุณภาพสูง พิมพ์ตามสั่ง"
                title_raw = title_raw + suffix
            title = title_raw[:120]

            # 商品名入力
            title_sel = (
                'input[placeholder*="Brand Name"], '
                'input[placeholder*="Key Features"], '
                'input[placeholder*="Product Name"], '
                'input[placeholder*="ชื่อสินค้า"], '
                'label:has-text("Product Name") ~ div input, '
                'label:has-text("Product Name") + div input'
            )
            try:
                self._page.wait_for_selector(title_sel, timeout=30000)
                _human_wait(0.5, 1.0)
                title_input = self._page.locator(title_sel).first
                title_input.click()
                _human_wait(0.3, 0.5)
                title_input.fill(title)
                logger.info(f"  商品名入力完了: {title[:30]}")
            except PlaywrightTimeout:
                inputs = self._page.locator("input:visible").all()
                logger.error(f"商品名入力欄が見つかりません（visible input数: {len(inputs)}）")
                self._screenshot(f"error_{mw_id}_title")
                return None

            _human_wait(1, 1.5)

            # 画像アップロード
            if not self._upload_images(local_image_paths):
                self._screenshot(f"error_{mw_id}_images")
                return None

            _human_wait(1, 2)

            # カテゴリチェックは Step2 に移動（Step1では locator が sidebar を拾うため不正確）

            # 「Next Step」ボタンをクリック（ポップアップ被りはforce=Trueで回避）
            # (_dismiss_listing_modals は呼ばない — Variation 誤活性化の原因になりうるため)
            next_sel = 'button:has-text("Next Step"), button:has-text("ถัดไป")'
            try:
                self._page.wait_for_selector(next_sel, timeout=8000)
                _human_wait(0.5, 1.0)
                # ポップアップをJS削除してからクリック
                self._page.evaluate("document.getElementById('sspSearchTour')?.remove()")
                self._page.locator(next_sel).first.click(force=True)
                self._page.wait_for_load_state("domcontentloaded", timeout=15000)
                logger.info("  Next Step クリック完了")
                # STEP2 フォームの出現を待つ（SPAなのでdomcontentloadedだけでは不十分）
                step2_found = False
                for step2_sel in [
                    'button:has-text("Save and Publish")',
                    'button:has-text("Save & Publish")',
                    ':text("Sales Information")',
                    ':text("Basic information")',
                    ':text("Basic Information")',
                ]:
                    try:
                        self._page.wait_for_selector(step2_sel, timeout=8000)
                        logger.info(f"  STEP2フォーム確認済み ({step2_sel})")
                        step2_found = True
                        break
                    except Exception:
                        continue
                if not step2_found:
                    logger.warning("  STEP2フォーム要素未確認 — 追加待機")
                    _human_wait(5, 7)
            except Exception as e:
                logger.warning(f"Next Step ボタンが見つかりません（続行）: {e}")
                self._screenshot(f"error_{mw_id}_nextstep")

            # ════════════════════════════════════════
            # STEP 2: タブ形式のフォームを順番に入力
            # タブ: Basic Info / Specification / Description / Sales Information / Shipping
            # ════════════════════════════════════════

            price = float(product.get("price_thb") or LISTING_SETTINGS["min_price_thb"])
            description = str(product.get("description_th") or "สินค้า 3D Printed คุณภาพสูง")[:3000]
            stock = LISTING_SETTINGS["default_stock"]
            weight_kg = max(0.1, (product.get("estimated_grams") or 100) / 1000)

            # カテゴリ選択は Basic Info タブ内（レンダリング後）で実行するため
            # ここでは定数だけ定義しておく
            CAT_BLOCKED = ["medical", "fda", "mom & baby", "stuffed toy", "sexual",
                           "wellness", "adult", "pharmaceutical", "health >",
                           "muslim", "hijab", "prayer", "baby >", "doll",
                           # ブランドライセンス必須カテゴリ（選択肢なし→必ず失敗）
                           "lighting", "vehicles", "motorcycle", "automotive",
                           "statues & sculptures", "statues", "figurines",
                           # Run 36 で Brand License 必須になった危険カテゴリ
                           "books", "careers", "self help", "religion",
                           "clips, pins", "large household appliances",
                           "console accessories", "gaming & consoles > others",
                           "home appliances > large",
                           # Run 37 で Brand License 必須と判明
                           "stones & minerals", "vehicle models", "diecast",
                           "pet furniture", "lanyards", "name tags",
                           "collectible items > stones",
                           "collectible items > vehicle"]
            CAT_PREF = {
                "tools": 3,
                "diy": 3,
                "hobbies": 3,
                "collectible": 2,  # Run37で collectible 配下がBrand License必須多発 → 優先度低下
                "arts": 3,
                "craft": 3,
                "sport": 1,
                "home & living": 1,
                "stationery": 1,
                "electronics": 0,
                "pets": -1,        # Pet Furniture が BL 必須
                "others": -1,
            }

            def _cat_score(txt: str) -> tuple:
                score = sum(weight for kw, weight in CAT_PREF.items() if kw in txt)
                is_others = txt.rstrip().endswith(" > others") or txt.rstrip().endswith(" > อื่นๆ")
                return score, is_others

            # ── Basic Info タブ（ブランド入力）────────
            # タブ名: "Basic information"（Shopee実際のラベル、小文字i）
            try:
                basic_tab = None
                for tab_name in ["Basic information", "Basic Information", "Basic Info"]:
                    t = self._page.get_by_text(tab_name, exact=True).first
                    if t.count():
                        basic_tab = t
                        logger.info(f"  Basic Info タブ発見: '{tab_name}'")
                        break
                if basic_tab:
                    basic_tab.click()
                    _human_wait(1.5, 2.5)
                    # 診断スクリーンショット
                    self._screenshot(f"debug_{mw_id}_basicinfo")

                    # ── カテゴリ選択（Basic Info タブ内、レンダリング後）────────────────
                    # ※ Recommended Categories は input[type="radio"] ではなくカスタム要素
                    #   sparkle-icon (<i class*="sparkle">) を起点に category パス要素を探す
                    try:
                        reco_info = self._page.evaluate("""
                            () => {
                                // sparkle icon を起点に Recommended Categories セクションを特定
                                const sparkle = document.querySelector('[class*="sparkle"]');
                                if (!sparkle) return [];
                                // sparkle の祖先を辿り、" > " を含む leaf 要素のあるコンテナを見つける
                                let container = sparkle.parentElement;
                                for (let lvl = 0; lvl < 8 && container && container !== document.body;
                                     lvl++, container = container.parentElement) {
                                    const items = [];
                                    for (const el of container.querySelectorAll('*')) {
                                        if (el === sparkle || el.contains(sparkle)) continue;
                                        if (el.children.length > 2) continue;
                                        const txt = el.textContent.trim();
                                        if (!txt.includes(' > ') || txt.length > 150) continue;
                                        const rect = el.getBoundingClientRect();
                                        if (!rect || rect.width < 50 || rect.height < 8) continue;
                                        items.push({ index: items.length, text: txt });
                                    }
                                    if (items.length > 0) return items;
                                }
                                return [];
                            }
                        """)
                        logger.info(f"  推奨カテゴリ一覧: {[(r['index'], r['text'][:60]) for r in reco_info]}")

                        best_idx = None
                        best_score = -1
                        best_is_others = True
                        for r in reco_info:
                            txt = r['text'].lower()
                            if any(kw in txt for kw in CAT_BLOCKED):
                                continue
                            score, is_others = _cat_score(txt)
                            # Others は最後の手段。非 Others 候補がある限り優先しない。
                            if best_idx is None:
                                best_score = score
                                best_idx = r['index']
                                best_is_others = is_others
                                continue
                            if best_is_others and not is_others:
                                best_score = score
                                best_idx = r['index']
                                best_is_others = is_others
                                continue
                            if is_others and not best_is_others:
                                continue
                            if score > best_score:
                                best_score = score
                                best_idx = r['index']
                                best_is_others = is_others
                            elif score == best_score and not is_others and best_is_others:
                                best_idx = r['index']
                                best_is_others = is_others

                        _do_pencil = False  # 推奨カテゴリ失敗 or なし → pencil edit フォールバック

                        if best_idx is not None:
                            # 安全な推奨カテゴリ → data属性でマークしてPlaywrightクリック
                            mark_result = self._page.evaluate(f"""
                                () => {{
                                    document.querySelectorAll('[data-cat-sel]').forEach(e => e.removeAttribute('data-cat-sel'));
                                    const sparkle = document.querySelector('[class*="sparkle"]');
                                    if (!sparkle) return null;
                                    let container = sparkle.parentElement;
                                    for (let lvl = 0; lvl < 8 && container && container !== document.body;
                                         lvl++, container = container.parentElement) {{
                                        const items = [];
                                        for (const el of container.querySelectorAll('*')) {{
                                            if (el === sparkle || el.contains(sparkle)) continue;
                                            if (el.children.length > 2) continue;
                                            const txt = el.textContent.trim();
                                            if (!txt.includes(' > ') || txt.length > 150) continue;
                                            const rect = el.getBoundingClientRect();
                                            if (!rect || rect.width < 50 || rect.height < 8) continue;
                                            items.push(el);
                                        }}
                                        if (items.length > 0) {{
                                            const el = items[{best_idx}];
                                            if (!el) return null;
                                            // 行全体をマーク（Playwrightクリック用）
                                            const row = el.closest('li, [class*="item"], [class*="row"]')
                                                        || el.parentElement;
                                            row.setAttribute('data-cat-sel', 'target');
                                            return el.textContent.trim().substring(0, 80);
                                        }}
                                    }}
                                    return null;
                                }}
                            """)
                            if mark_result:
                                # Playwrightのクリック（実際のマウスイベント、Reactに確実に届く）
                                try:
                                    self._page.locator('[data-cat-sel="target"]').first.click(timeout=5000)
                                except Exception:
                                    # フォールバック: dispatchEvent
                                    self._page.evaluate("""
                                        () => {
                                            const el = document.querySelector('[data-cat-sel="target"]');
                                            if (el) el.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true,view:window}));
                                        }
                                    """)
                                logger.info(f"  ✅ 推奨カテゴリ選択: {mark_result}")
                                # Enter キーでカテゴリ選択を確定させる（Vueが単一クリックだけでは未コミットの場合）
                                try:
                                    _human_wait(0.3, 0.5)
                                    self._page.keyboard.press("Enter")
                                except Exception:
                                    pass
                            _human_wait(3.0, 4.0)  # Vue 再描画 + カテゴリ確定待ち（長めに）
                            # ── カテゴリ変更確認ダイアログを処理 ──────────────────────
                            # "Changing category will clear product info" → Confirm が必要
                            _cat_confirmed = False
                            for _cconf_sel in [
                                'button:has-text("Confirm")',
                                'button:has-text("ยืนยัน")',
                                '[role="dialog"] button:last-child',
                                '[class*="modal"] button:last-child',
                            ]:
                                try:
                                    _cconf = self._page.locator(_cconf_sel).first
                                    if _cconf.count() and _cconf.is_visible():
                                        _cconf.click()
                                        logger.info(f"  カテゴリ確認ダイアログ: Confirm クリック ({_cconf_sel})")
                                        _cat_confirmed = True
                                        _human_wait(1.5, 2.0)
                                        break
                                except Exception:
                                    pass
                            if _cat_confirmed:
                                _human_wait(1.0, 1.5)  # ダイアログ消去 + Vue 再描画待ち
                            # カテゴリ選択確認（eds-selectors が増えるか形式が変わるか）
                            cat_ok = self._page.evaluate("""
                                () => {
                                    const eds = [...document.querySelectorAll('[class*="eds-selector"]')]
                                        .filter(e => e.offsetParent !== null);
                                    // カテゴリが選択されると Brand selector が出る(totalEds>0)
                                    // または "Please set category" が消える
                                    const catPlaceholder = document.querySelector('[placeholder*="category"], [placeholder*="Category"]');
                                    const catText = [...document.querySelectorAll('[class*="category"] [class*="value"], [class*="category-path"]')]
                                        .find(e => e.textContent.trim().includes('>'));
                                    return eds.length > 0 || catText !== undefined;
                                }
                            """)
                            if not cat_ok:
                                logger.warning("  ⚠️ カテゴリ未選択（フォーム未更新） → 再クリック試行")
                                # セカンド試行: 直接 JS click
                                self._page.evaluate("""
                                    () => {
                                        const el = document.querySelector('[data-cat-sel="target"]');
                                        if (el) {
                                            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true,cancelable:true,view:window}));
                                            el.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true,cancelable:true,view:window}));
                                            el.dispatchEvent(new MouseEvent('click',     {bubbles:true,cancelable:true,view:window}));
                                        }
                                    }
                                """)
                                _human_wait(2.0, 3.0)
                                # 2回目確認 — まだ未コミットならpencil editへ
                                _cat_ok_r2 = self._page.evaluate("""
                                    () => {
                                        const eds = [...document.querySelectorAll('[class*="eds-selector"]')]
                                            .filter(e => e.offsetParent !== null);
                                        const catText = [...document.querySelectorAll(
                                            '[class*="category"] [class*="value"], [class*="category-path"]'
                                        )].find(e => e.textContent.trim().includes('>'));
                                        return eds.length > 0 || catText !== undefined;
                                    }
                                """)
                                if not _cat_ok_r2:
                                    logger.warning("  ⚠️ 推奨カテゴリ2回試行でも未コミット → pencil edit フォールバック")
                                    _do_pencil = True
                        else:
                            # 全推奨がブロック or 推奨なし → pencil edit
                            _do_pencil = True
                            logger.info("  推奨カテゴリに安全なものなし → pencil edit で変更")
                        if _do_pencil:
                            try:
                                # カテゴリ picker を開く
                                # 方法1: "Please set category" プレースホルダを直接クリック
                                # 方法2: sparkle の前の pencil SVG/アイコン
                                pencil_clicked = self._page.evaluate("""
                                    () => {
                                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                                        let node;
                                        while (node = walker.nextNode()) {
                                            const txt = node.textContent.trim();
                                            if ((txt === 'Please set category' || txt === 'Please Select Category'
                                                    || txt === 'โปรดเลือกหมวดหมู่')
                                                    && node.parentElement.offsetParent !== null) {
                                                const el = node.parentElement;
                                                el.click();
                                                return 'placeholder: ' + el.tagName + '.' + (el.className || '').substring(0, 40);
                                            }
                                        }
                                        // フォールバック: sparkle 前の SVG アイコン
                                        const sparkle = document.querySelector('[class*="sparkle"]');
                                        const candidates = [...document.querySelectorAll('svg, i[class*="icon"]')]
                                            .filter(el => {
                                                const rect = el.getBoundingClientRect();
                                                if (!rect || rect.width === 0) return false;
                                                const cls = (el.className?.baseVal || el.getAttribute?.('class') || '').toLowerCase();
                                                if (cls.includes('sparkle')) return false;
                                                if (sparkle) {
                                                    const pos = sparkle.compareDocumentPosition(el);
                                                    if (!(pos & 2)) return false;
                                                }
                                                return true;
                                            });
                                        if (candidates.length === 0) return null;
                                        const icon = candidates[candidates.length - 1];
                                        const target = icon.closest('button, a, [role="button"]') || icon.parentElement;
                                        target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                                        const cls = (icon.className?.baseVal || icon.getAttribute?.('class') || '');
                                        return 'icon: ' + icon.tagName + '.' + cls.substring(0, 40);
                                    }
                                """)
                                logger.info(f"  pencil click: {pencil_clicked}")
                                _human_wait(2.0, 3.0)
                                self._screenshot(f"debug_{mw_id}_cat_modal")

                                # ── Recently Used ショートカット ────────────────────────
                                # picker が「最近使用」ドロップダウンを開いた場合に直接選択
                                _ru_selected = False
                                try:
                                    _visible_cat_paths = self._page.evaluate("""
                                        () => {
                                            const results = [];
                                            const walker = document.createTreeWalker(
                                                document.body, NodeFilter.SHOW_TEXT, null
                                            );
                                            let node;
                                            while (node = walker.nextNode()) {
                                                const el = node.parentElement;
                                                if (!el || el.offsetParent === null) continue;
                                                const txt = node.textContent.trim();
                                                if (txt.includes(' > ') && txt.length > 8 && txt.length < 200)
                                                    results.push(txt);
                                            }
                                            return [...new Set(results)];
                                        }
                                    """)
                                    logger.info(f"  picker カテゴリ候補: {_visible_cat_paths}")
                                    for _rc in (_visible_cat_paths or []):
                                        if any(kw in _rc.lower() for kw in CAT_BLOCKED):
                                            logger.info(f"  picker スキップ（ブロック）: {_rc[:60]}")
                                            continue
                                        try:
                                            self._page.get_by_text(_rc, exact=True).first.click(timeout=2000)
                                            logger.info(f"  ✅ picker カテゴリ選択: {_rc[:60]}")
                                            _ru_selected = True
                                            _human_wait(2.0, 3.0)
                                            break
                                        except Exception:
                                            pass
                                except Exception as _ru_e:
                                    logger.debug(f"  picker shortcut エラー（無視）: {_ru_e}")

                                if not _ru_selected:
                                    # フルツリーモーダル ナビゲーション
                                    # 推奨カテゴリパスから動的にナビパスを取得（ブロックされていないもの）
                                    _nav_parts = None
                                    for _rc in (_visible_cat_paths or []):
                                        if not any(kw in _rc.lower() for kw in CAT_BLOCKED):
                                            parts = [p.strip() for p in _rc.split(' > ')]
                                            if len(parts) >= 2:
                                                _nav_parts = parts
                                                logger.info(f"  動的ナビパス: {' > '.join(_nav_parts)}")
                                                break

                                    if _nav_parts:
                                        # 推奨カテゴリパスを使って動的ツリーナビ
                                        for _li, _cat_name in enumerate(_nav_parts):
                                            _lv = _li + 1
                                            _lv_clicked = False
                                            if _lv < len(_nav_parts):
                                                # L1/L2: exact click
                                                try:
                                                    self._page.get_by_text(_cat_name, exact=True).first.click(timeout=3000)
                                                    logger.info(f"  カテゴリツリー L{_lv}: {_cat_name}")
                                                    _lv_clicked = True
                                                    _human_wait(0.8, 1.2)
                                                except Exception:
                                                    logger.warning(f"  カテゴリツリー L{_lv}: '{_cat_name}' not found")
                                            else:
                                                # L3（最終）: nth 順に試す
                                                for nth in range(1, 6):
                                                    try:
                                                        self._page.get_by_text(_cat_name, exact=True).nth(nth).click(timeout=2000)
                                                        logger.info(f"  カテゴリツリー L{_lv}: {_cat_name} [nth={nth}]")
                                                        _lv_clicked = True
                                                        break
                                                    except Exception:
                                                        pass
                                                if not _lv_clicked:
                                                    # 最終LV: "Others" で代替
                                                    for _alt in ["Others", "อื่นๆ"]:
                                                        for nth in range(1, 6):
                                                            try:
                                                                self._page.get_by_text(_alt, exact=True).nth(nth).click(timeout=2000)
                                                                logger.info(f"  カテゴリツリー L{_lv}: {_alt} [nth={nth}] (代替)")
                                                                _lv_clicked = True
                                                                break
                                                            except Exception:
                                                                pass
                                                        if _lv_clicked:
                                                            break
                                    else:
                                        # フォールバック: 固定候補リスト
                                        for level, candidates in [
                                            (1, ["งานอดิเรก", "Hobbies & Collections", "Hobbies",
                                                 "งานอดิเรกและของสะสม", "Home & Living",
                                                 "บ้านและชีวิตประจำวัน", "Tools", "Sports"]),
                                            (2, ["Collectible Items", "Collectible", "Hobby Supplies",
                                                 "Gardening", "Garden Supplies", "Kitchen & Dining",
                                                 "Home Decor", "DIY", "ของสะสม"]),
                                            (3, ["Others", "อื่นๆ"]),
                                        ]:
                                            level_clicked = False
                                            for txt in candidates:
                                                if level_clicked:
                                                    break
                                                if level == 3:
                                                    for nth in range(1, 6):
                                                        try:
                                                            self._page.get_by_text(txt, exact=True).nth(nth).click(timeout=2000)
                                                            logger.info(f"  カテゴリツリー L{level}: {txt} [nth={nth}]")
                                                            level_clicked = True
                                                            break
                                                        except Exception:
                                                            pass
                                                else:
                                                    try:
                                                        self._page.get_by_text(txt, exact=True).first.click(timeout=3000)
                                                        logger.info(f"  カテゴリツリー L{level}: {txt}")
                                                        level_clicked = True
                                                    except Exception:
                                                        pass
                                            if not level_clicked:
                                                logger.warning(f"  カテゴリツリー L{level}: 候補なし（スキップ）")
                                            _human_wait(0.8, 1.2)

                                    # Confirm ボタン
                                    _human_wait(0.5, 1.0)
                                    confirm_clicked = False
                                    try:
                                        self._page.get_by_text("Confirm", exact=True).first.click(timeout=5000)
                                        confirm_clicked = True
                                    except Exception:
                                        pass
                                    if not confirm_clicked:
                                        try:
                                            self._page.get_by_text("ยืนยัน", exact=True).first.click(timeout=3000)
                                            confirm_clicked = True
                                        except Exception:
                                            pass
                                    if confirm_clicked:
                                        _human_wait(1.5, 2.0)
                                        logger.info("  ✅ カテゴリ変更 (pencil) 完了")
                                    else:
                                        logger.warning("  pencil edit: Confirm ボタン未発見")
                            except Exception as e:
                                logger.warning(f"  pencil edit エラー（続行）: {e}")
                    except Exception as e:
                        logger.warning(f"  カテゴリ選択エラー（続行）: {e}")

                    # ── カテゴリ非対応の早期検出 ──────────────────────────
                    # "Selected category is not supported" バナーが出た場合は
                    # 全フォームを埋めても失敗するため、ここで中断する
                    _human_wait(0.5, 1.0)
                    cat_error = self._page.evaluate("""
                        () => {
                            const txt = document.body.innerText || '';
                            if (txt.includes('Selected category is not supported') ||
                                txt.includes('category is not supported')) {
                                return true;
                            }
                            const banner = document.querySelector(
                                '[class*="error-banner"], [class*="alert-error"], .error-message'
                            );
                            return banner ? banner.textContent.includes('not supported') : false;
                        }
                    """)
                    if cat_error:
                        logger.error("❌ カテゴリ非対応 — このカテゴリはこのセラーでは出品不可。スキップします")
                        self._screenshot(f"cat_unsupported_{mw_id}")
                        return None

                    # Brand フィールドは Specification タブの先頭にある
                    # → Basic Info タブがアクティブな間は Specification セクションが
                    #   v-show="false" で非表示のため、先に Specification タブをクリックする
                    brand_filled = False
                    _spec_tab_activated_for_brand = False  # Specタブを既にアクティブ化したか追跡
                    try:
                        # Specification タブをクリック（Brand は Specification セクションの先頭）
                        # get_by_text が複数マッチする場合 JS fallback でタブを確実に切り替える
                        spec_clicked = self._page.evaluate("""
                            () => {
                                const tabs = [...document.querySelectorAll(
                                    '[class*="tabs__nav-tab"], [class*="tab-item"], ' +
                                    '[class*="tab-nav"], [role="tab"]'
                                )];
                                const spec = tabs.find(el =>
                                    el.textContent.trim().startsWith('Specification'));
                                if (spec) { spec.click(); return true; }
                                return false;
                            }
                        """)
                        if not spec_clicked:
                            _spec_tab_early = self._page.get_by_text("Specification", exact=True).first
                            if _spec_tab_early.count():
                                _spec_tab_early.click()
                        _spec_tab_activated_for_brand = True  # Specタブをアクティブ化した
                        _human_wait(1.5, 2.0)  # セクションが表示されるまで待つ
                        # ページ最上部にスクロール（Brandフィールドは常にSpecificationの先頭）
                        self._page.evaluate("window.scrollTo(0, 0)")
                        _human_wait(0.5, 1.0)

                        # JS evaluate でクリック
                        # ── TreeWalker でテキストノード "Brand" を直接探し EDS selector へ ──
                        # 以前の children.length==0 フィルタはラベルに子 span(*印) がある場合に
                        # 誤って除外する。TreeWalker はテキストノード(=常にleaf)を直接走査するため
                        # 子要素数に左右されない。またparent.textContent の代わりに先祖単位で
                        # 検索することで "Brand License" と誤マッチする問題も解消する。
                        brand_click = self._page.evaluate("""
                            () => {
                                // ① TreeWalker でテキストノード "Brand" を探す
                                // テキストノードに children はないため children.length 制約不要
                                const walker = document.createTreeWalker(
                                    document.body, NodeFilter.SHOW_TEXT);
                                let node;
                                const brandNodes = [];
                                while (node = walker.nextNode()) {
                                    const raw = node.textContent.trim();
                                    const clean = raw.replace(/[*\\s]/g, '');
                                    // "Brand" のみ。"BrandLicense" は除外
                                    if (clean !== 'Brand') continue;
                                    const par = node.parentElement;
                                    if (!par || par.offsetParent === null) continue;
                                    brandNodes.push(par);
                                }
                                // 各 Brand テキストノードの親から上に向かって EDS selector を探す
                                for (const par of brandNodes) {
                                    let container = par;
                                    for (let i = 0; i < 12; i++) {
                                        if (!container) break;
                                        const sel = container.querySelector('[class*="eds-selector"]');
                                        if (sel && sel.offsetParent !== null) {
                                            sel.scrollIntoView({block: 'center'});
                                            sel.click();
                                            return {found: true, method: 'textnode_walk',
                                                    txt: par.textContent.trim().substring(0, 30),
                                                    level: i};
                                        }
                                        container = container.parentElement;
                                    }
                                }
                                // ② EDS selector から逆に先祖を辿り Brand ラベルを確認
                                // parent.textContent でなく各先祖の直下 children のテキストを確認
                                // → Brand と Brand License が同一親に居ても先祖レベルで切り分け
                                const allEds = [...document.querySelectorAll('[class*="eds-selector"]')]
                                    .filter(el => el.offsetParent !== null);
                                for (const eds of allEds) {
                                    let ancestor = eds.parentElement;
                                    for (let depth = 0; depth < 8; depth++) {
                                        if (!ancestor) break;
                                        // この先祖の直接の子要素のテキストに "Brand" があり
                                        // "Brand License" / "Variation" / "Lighting" が含まれないか確認
                                        for (const child of ancestor.children) {
                                            const ct = child.textContent.trim().replace(/\\s+/g,' ');
                                            if (ct.includes('Brand')
                                                    && !ct.includes('Brand License')
                                                    && !ct.includes('Variation')
                                                    && !ct.includes('Shipping')
                                                    && ct.length < 30) {
                                                eds.scrollIntoView({block: 'center'});
                                                eds.click();
                                                return {found: true, method: 'ancestor_child',
                                                        depth, labelText: ct.substring(0, 30)};
                                            }
                                        }
                                        ancestor = ancestor.parentElement;
                                    }
                                }
                                // ③ フォールバック: Specification セクション内の最初の EDS selector
                                // Brand フィールドは常にSpecificationの先頭
                                const specSec = document.querySelector(
                                    '[class*="specification"], [class*="spec-section"], ' +
                                    '[class*="product-specification"]'
                                );
                                if (specSec) {
                                    const eds = specSec.querySelector('[class*="eds-selector"]');
                                    if (eds && eds.offsetParent !== null) {
                                        eds.scrollIntoView({block: 'center'});
                                        eds.click();
                                        return {found: true, method: 'spec_section_first',
                                                secClass: specSec.className.substring(0, 60)};
                                    }
                                }
                                // デバッグ情報を返す
                                const firstEdsCtx = allEds.length > 0
                                    ? (allEds[0].closest('[class*="form-item"], [class*="field"]')
                                        || allEds[0].parentElement
                                      )?.textContent?.trim()?.substring(0, 80)
                                    : 'none';
                                return {found: false, totalEds: allEds.length,
                                        brandNodesFound: brandNodes.length,
                                        firstEdsCtx: firstEdsCtx,
                                        reason: 'no brand selector found'};
                            }
                        """)
                        logger.info(f"  Brand JS click: {brand_click}")

                        if brand_click.get("found"):
                            _human_wait(0.8, 1.2)
                            # "No Brand" オプションを選択
                            for opt_sel in [
                                'li:has-text("No Brand")',
                                '[role="option"]:has-text("No Brand")',
                                '[class*="option"]:has-text("No Brand")',
                                '[class*="popover"] li:first-child',
                                '[class*="dropdown"] li:first-child',
                            ]:
                                try:
                                    opt = self._page.locator(opt_sel).first
                                    if opt.count() and opt.is_visible():
                                        opt.click()
                                        logger.info(f"  ブランド選択: No Brand (via {opt_sel})")
                                        brand_filled = True
                                        _human_wait(0.5, 1.0)
                                        break
                                except Exception:
                                    pass

                            if not brand_filled:
                                # Dropdown に search input がある場合 "No Brand" と入力
                                search_inp = self._page.locator(
                                    '[class*="popover"] input, [class*="dropdown"] input'
                                ).first
                                if search_inp.count() and search_inp.is_visible():
                                    search_inp.fill("No Brand")
                                    _human_wait(0.5, 1.0)
                                    opt = self._page.get_by_text("No Brand", exact=True).first
                                    if opt.count() and opt.is_visible():
                                        opt.click()
                                        brand_filled = True
                                        logger.info("  ブランド選択: No Brand (via search input)")
                    except Exception as e:
                        logger.warning(f"  ブランド入力エラー（続行）: {e}")

                    if not brand_filled:
                        logger.warning("  ⚠️ ブランドを設定できませんでした")

                    # ── ブランド選択確認 + Brand License ──────────────
                    # Brand が "No Brand" 以外になっていた場合（single_selector誤選択等）
                    # Brand License ドロップダウンを選択してフォームバリデーションを通過させる
                    try:
                        _human_wait(0.5, 0.8)
                        brand_license_result = self._page.evaluate("""
                            () => {
                                // TreeWalker でテキストノードを直接探す（children.length制約なし）
                                const findTextNode = (searchText, excludeText) => {
                                    const walker = document.createTreeWalker(
                                        document.body, NodeFilter.SHOW_TEXT);
                                    let node;
                                    while (node = walker.nextNode()) {
                                        const raw = node.textContent.trim();
                                        if (!raw.includes(searchText)) continue;
                                        if (excludeText && raw.includes(excludeText)) continue;
                                        const par = node.parentElement;
                                        if (!par || par.offsetParent === null) continue;
                                        return par;
                                    }
                                    return null;
                                };

                                // Brand の現在値を読み取る
                                // 方法1: form-item の label "Brand" から eds-selector を探す（最も信頼性が高い）
                                let currentBrand = null;
                                const allItems = [...document.querySelectorAll('[class*="form-item"]')];
                                for (const item of allItems) {
                                    const lbl = item.querySelector(
                                        'label, [class*="form-item__label"], [class*="label"]'
                                    );
                                    if (!lbl) continue;
                                    const lblTxt = lbl.textContent.trim().replace(/[*\\s]/g,'');
                                    if (lblTxt !== 'Brand' && lblTxt !== 'แบรนด์') continue;
                                    const sel = item.querySelector('[class*="eds-selector"]');
                                    if (!sel) continue;
                                    // EDS selector の表示テキストを直接読む（最も確実）
                                    const ph = sel.querySelector('[class*="placeholder"]');
                                    const phTxt = ph ? ph.textContent.trim() : '';
                                    const rawTxt = sel.textContent.trim();
                                    if (rawTxt && rawTxt !== phTxt && rawTxt !== 'Please select' && rawTxt !== 'โปรดเลือก') {
                                        currentBrand = rawTxt;
                                    }
                                    break;
                                }
                                // 方法2: フォールバック — TreeWalker で "Brand" ラベルを探す
                                if (currentBrand === null) {
                                    const brandEl = findTextNode('Brand', 'License');
                                    if (brandEl) {
                                        let c = brandEl;
                                        for (let i = 0; i < 10; i++) {
                                            if (!c) break;
                                            const sel = c.querySelector('[class*="eds-selector"]');
                                            if (sel) {
                                                const ph = sel.querySelector('[class*="placeholder"]');
                                                const phTxt = ph ? ph.textContent.trim() : '';
                                                const rawTxt = sel.textContent.trim();
                                                if (rawTxt && rawTxt !== phTxt && rawTxt !== 'Please select') {
                                                    currentBrand = rawTxt;
                                                }
                                                break;
                                            }
                                            c = c.parentElement;
                                        }
                                    }
                                }

                                // Brand License ラベルを探す (TreeWalker)
                                const licEl = findTextNode('Brand License', null);
                                if (!licEl) return {brand: currentBrand, licenseHandled: 'no-label'};

                                // Brand License の EDS selector を探す
                                let lc = licEl;
                                for (let i = 0; i < 10; i++) {
                                    if (!lc) break;
                                    const sel = lc.querySelector('[class*="eds-selector"]');
                                    if (sel) {
                                        const ph = sel.querySelector('[class*="placeholder"]');
                                        const isUnset = ph && ph.offsetParent !== null;
                                        if (!isUnset) {
                                            return {brand: currentBrand, licenseHandled: 'already-set'};
                                        }
                                        sel.scrollIntoView({block: 'center'});
                                        sel.click();
                                        return {brand: currentBrand, licenseHandled: 'clicked'};
                                    }
                                    lc = lc.parentElement;
                                }
                                return {brand: currentBrand, licenseHandled: 'selector-not-found'};
                            }
                        """)
                        logger.info(f"  Brand確認: brand={brand_license_result.get('brand')}, license={brand_license_result.get('licenseHandled')}")

                        if brand_license_result.get('licenseHandled') == 'clicked':
                            _human_wait(0.8, 1.2)
                            # Brand License ドロップダウンが開いた → 最初の選択肢を選ぶ
                            lic_opt = self._page.evaluate("""
                                () => {
                                    const isVis = (el) => {
                                        const r = el.getBoundingClientRect();
                                        return r.width > 0 && r.height > 0;
                                    };
                                    const opts = [...document.querySelectorAll(
                                        '[class*="eds-select__options"] [class*="option"], ' +
                                        '[class*="eds-option"]:not([class*="option-add"]), ' +
                                        '[class*="popover"] li'
                                    )].filter(el => isVis(el)
                                        && !el.closest('[class*="form-item"]'));
                                    if (opts.length > 0) {
                                        opts[0].click();
                                        return opts[0].textContent.trim().slice(0, 60);
                                    }
                                    return null;
                                }
                            """)
                            if lic_opt:
                                logger.info(f"  Brand License選択: '{lic_opt}'")
                            else:
                                self._page.keyboard.press("Escape")
                                logger.info("  Brand License: 選択肢なし → Escape")
                                # ── Brand License必須判定 ──────────────────────────────
                                # Brand が特定ブランド（例: ACDelco, Nation Edutainment）に
                                # 強制設定され、かつ選択肢がない場合のみスキップ。
                                # Brand=None/unknown は「No Brand」選択後の確認UIの可能性が高い
                                # → その場合は続行してサーバーバリデーションに委ねる
                                _cur_brand = brand_license_result.get('brand') or ''
                                _no_brand_vals = ('', 'unknown', 'No Brand', 'ไม่มีแบรนด์', 'None')
                                if _cur_brand not in _no_brand_vals:
                                    # ブランドが特定ブランドに強制設定 → ライセンス必須 → スキップ
                                    logger.error(
                                        f"  ❌ ブランドライセンス必須カテゴリ: Brand='{_cur_brand}' "
                                        f"→ ブランド登録なしで出品不可 → スキップ"
                                    )
                                    update_status(
                                        mw_id, 'error',
                                        error_msg=f'brand_license_required:{_cur_brand[:30]}'
                                    )
                                    return None
                                else:
                                    # Brand=None/No Brand → Brand License確認UIの可能性
                                    # Escapeして続行（サーバーバリデーションに委ねる）
                                    logger.info(
                                        f"  Brand License: Brand='{_cur_brand}' → 続行 "
                                        f"（サーバー検証に委ねる）"
                                    )
                    except Exception as e:
                        logger.debug(f"  Brand License処理エラー（続行）: {e}")
                else:
                    logger.warning("  ⚠️ Basic Info タブが見つかりません")
            except Exception as e:
                logger.warning(f"  Basic Infoタブエラー（続行）: {e}")

            _human_wait(0.8, 1.2)

            # ── Specification タブ（属性）────────────
            try:
                spec_tab = self._page.get_by_text("Specification", exact=True).first
                if spec_tab.count():
                    # Brand選択フェーズで既にSpecificationタブをアクティブ化済みの場合は
                    # 再クリックを避ける。再クリックするとVue.jsがBrandを初期値(≠No Brand)にリセットする
                    if not _spec_tab_activated_for_brand:
                        spec_tab.click()
                        _human_wait(1.5, 2.5)
                    else:
                        logger.info("  Specタブ: Brand選択フェーズで既にアクティブ化済み → 再クリックスキップ（Brand保護）")
                        _human_wait(0.3, 0.5)
                    self._screenshot(f"debug_{mw_id}_specification")

                    # 可視の入力フィールドを診断
                    spec_info = self._page.evaluate("""
                        () => {
                            const inputs = [...document.querySelectorAll('input')]
                                .filter(i => i.offsetParent !== null && !i.readOnly && !i.disabled
                                            && i.type !== 'radio' && i.type !== 'checkbox');
                            const selectors = [...document.querySelectorAll('[class*="eds-selector"]')]
                                .filter(el => el.offsetParent !== null);
                            return {
                                inputCount: inputs.length,
                                selectorCount: selectors.length,
                                firstInputPh: inputs[0]?.placeholder || '',
                                firstInputVal: inputs[0]?.value || '',
                            };
                        }
                    """)
                    logger.info(f"  Specification状態: {spec_info}")

                    # テキスト入力欄を汎用値で埋める（必須属性の可能性）
                    # 制限を 20 に拡大: TIS No./Website などカテゴリ固有必須フィールドも対象
                    # "Show more" がある場合はスクロールして展開する
                    try:
                        show_more = self._page.get_by_text("Show more", exact=False).first
                        if show_more.count() and show_more.is_visible():
                            show_more.click()
                            _human_wait(0.5, 1.0)
                    except Exception:
                        pass
                    filled_count = self._page.evaluate("""
                        () => {
                            const setter = Object.getOwnPropertyDescriptor(
                                HTMLInputElement.prototype, 'value').set;
                            // 対象: eds-input, number input, text input, 汎用class付きinput
                            // (TIS No./Input Voltage 等は type="number", TIS website は type="text")
                            const inputs = [...document.querySelectorAll(
                                'input.eds-input__input, input[type="number"], input[type="text"], ' +
                                'input:not([type])[class]'
                            )].filter(i => i.offsetParent !== null && !i.readOnly && !i.disabled
                                        && i.type !== 'radio' && i.type !== 'checkbox'
                                        && !i.value);
                            // ラベルを取得して TIS/URL/number フィールドを判別
                            const getLabel = (inp) => {
                                let el = inp.parentElement;
                                for (let i = 0; i < 8; i++) {
                                    if (!el) break;
                                    const lbl = el.querySelector(
                                        'label, [class*="label"], [class*="form-item__label"]'
                                    );
                                    if (lbl && lbl !== inp) return lbl.textContent.toLowerCase();
                                    el = el.parentElement;
                                }
                                return '';
                            };
                            let filled = 0;
                            for (const inp of inputs) {
                                const ph  = (inp.placeholder || '').toLowerCase();
                                const lbl = getLabel(inp);
                                // 除外: ブランド名・商品名・カラー系フィールド
                                if (ph.includes('brand') || ph.includes('product') ||
                                    ph.includes('name') || ph.includes('color') ||
                                    ph.includes('e.g. color') || ph.includes('e.g. red') ||
                                    ph.includes('sku')) continue;
                                // フィールドタイプをラベルとプレースホルダで判別
                                let val;
                                if (ph.includes('http') || ph.includes('url') ||
                                    ph.includes('website') || ph.includes('link') ||
                                    ph.includes('site') ||
                                    lbl.includes('website') || lbl.includes('url')) {
                                    val = 'https://app.tisi.go.th/ulprod/certSearch.jsp';
                                } else if (ph.includes('no.') || ph.includes('number') ||
                                           ph.includes('certificate') || ph.includes('#') ||
                                           ph.includes('code') || ph.includes('id') ||
                                           ph.includes('tis') ||
                                           lbl.includes('tis') || lbl.includes('certificate') ||
                                           lbl.includes('no.') || lbl.includes('fda') ||
                                           inp.type === 'number') {
                                    val = '12345';
                                } else {
                                    val = '3D Printed Plastic';
                                }
                                setter.call(inp, val);
                                inp.dispatchEvent(new Event('input', {bubbles: true}));
                                inp.dispatchEvent(new Event('change', {bubbles: true}));
                                inp.blur();
                                filled++;
                                if (filled >= 25) break;  // 最大25フィールド
                            }
                            return filled;
                        }
                    """)
                    if filled_count > 0:
                        logger.info(f"  Specification: テキスト属性 {filled_count} 件入力")

                    # EDS ドロップダウン（全空セレクタを1件ずつ選択）
                    # 注: Shopeeの必須マーク(*)はCSS ::before で描画されDOMテキストに含まれない
                    # data-spec-tried属性で試行済みマークを付けて無限ループを防ぐ
                    self._page.evaluate("() => document.querySelectorAll('[data-spec-tried]').forEach(e => e.removeAttribute('data-spec-tried'))")
                    for _iter in range(20):   # 最大20回試みる
                        # 次の未試行・未入力のドロップダウンを探す
                        _next = self._page.evaluate("""
                            () => {
                                const emptyTxts = new Set(['Please select','Select','โปรดเลือก','']);
                                const allItems = [...document.querySelectorAll('[class*="form-item"]')];
                                // 必須クラスを持つitem優先
                                const prioritized = [
                                    ...allItems.filter(i =>
                                        i.className.includes('required') ||
                                        (i.querySelector('[class*="label"]') || {}).className?.includes('required')
                                    ),
                                    ...allItems
                                ];
                                for (const item of prioritized) {
                                    const sel = item.querySelector('[class*="eds-selector"]');
                                    if (!sel) continue;
                                    // offsetParent===nullはposition:fixedで発生するので使用しない
                                    const r = sel.getBoundingClientRect();
                                    if (r.width === 0 || r.height === 0) continue;
                                    if (sel.dataset.specTried) continue;   // 試行済みはスキップ
                                    const txt = sel.textContent.trim();
                                    if (!emptyTxts.has(txt)) continue;
                                    // Brand / Brand License フィールドは brand-click で処理済み → スキップ
                                    // 方法1: form-item 内ラベルテキストで判定
                                    {
                                        const _lbl0 = item.querySelector(
                                            'label, [class*="form-item__label"], [class*="label"]'
                                        );
                                        if (_lbl0) {
                                            const _t = _lbl0.textContent.trim().replace(/[*\\s]/g,'');
                                            if (_t === 'Brand' || _t === 'BrandLicense') continue;
                                        }
                                    }
                                    // 方法2: sel 近傍4階層以内の TextWalker で "Brand" のみのノードを探す
                                    // ラベルクラス名が "label" を含まない場合のフォールバック
                                    {
                                        let _isBrand = false;
                                        let _anc = sel.parentElement;
                                        for (let _ai = 0; _ai < 4 && _anc && !_isBrand; _ai++, _anc = _anc.parentElement) {
                                            const _wk = document.createTreeWalker(_anc, NodeFilter.SHOW_TEXT);
                                            let _tn;
                                            while ((_tn = _wk.nextNode())) {
                                                const _tc = _tn.textContent.trim().replace(/[*\\s]/g,'');
                                                if (_tc === 'Brand') { _isBrand = true; break; }
                                            }
                                        }
                                        if (_isBrand) continue;
                                    }
                                    sel.dataset.specTried = '1';           // マーク
                                    sel.scrollIntoView({block: 'center', inline: 'nearest'});
                                    sel.click();
                                    // ラベルテキスト取得（複数パターン試行）
                                    const lbl = item.querySelector(
                                        'label, [class*="form-item__label"], [class*="label"]'
                                    );
                                    const lblTxt = lbl
                                        ? [...lbl.childNodes]
                                            .filter(n => n.nodeType === 3)
                                            .map(n => n.textContent.trim())
                                            .join(' ').trim() || lbl.textContent.trim()
                                        : 'unknown';
                                    return lblTxt.replace(/\\*/g,'').trim() || 'unknown';
                                }
                                return null;
                            }
                        """)
                        if not _next:
                            break
                        _human_wait(1.2, 1.8)   # ドロップダウンのロードを待つ
                        # 開いたドロップダウンの最初の有効オプションを選択
                        # key insight: recommendation chipsは[class*="form-item"]の中にある
                        # EDS dropdownオプションはform-itemの「外」に描画される
                        _selected = self._page.evaluate("""
                            () => {
                                const isBad = (txt) => {
                                    if (!txt || txt.length === 0 || txt.length > 100) return true;
                                    if (txt.includes(' > ')) return true;
                                    if (txt.includes('Add a new')) return true;
                                    if (txt.includes('Please input')) return true;
                                    if (txt.includes('Please select')) return true;
                                    if (txt.includes('Loading')) return true;
                                    if (txt.includes('No Result')) return true;
                                    if (txt.includes('Recommended Value')) return true;
                                    if (txt.includes('Self-fill')) return true;
                                    if (txt.includes('โปรดเลือก')) return true;
                                    if (/^[\\s\\n]+$/.test(txt)) return true;
                                    return false;
                                };
                                const isVis = (el) => {
                                    const r = el.getBoundingClientRect();
                                    return r.width > 0 && r.height > 0
                                        && r.top >= 0 && r.top < window.innerHeight;
                                };

                                // strategy A: [class*="option"] のうち form-item の「外」にあるもの
                                // → EDS dropdown popup は form-item 外にレンダリングされる
                                const allOpts = [...document.querySelectorAll('[class*="option"]')]
                                    .filter(el => {
                                        if (!isVis(el)) return false;
                                        const txt = el.textContent.trim();
                                        if (isBad(txt)) return false;
                                        // form-item の中（推奨チップ）は除外
                                        if (el.closest('[class*="form-item"], [class*="eds-form-item"]')) return false;
                                        return true;
                                    });
                                if (allOpts.length > 0) {
                                    allOpts[0].click();
                                    return 'out-of-form:' + allOpts[0].textContent.trim().substring(0, 40);
                                }

                                // strategy B: form-item 内でも optional-item クラスを除外
                                const inFormOpts = [...document.querySelectorAll('[class*="option"]:not([class*="optional"])')]
                                    .filter(el => {
                                        if (!isVis(el)) return false;
                                        const txt = el.textContent.trim();
                                        return !isBad(txt);
                                    });
                                if (inFormOpts.length > 0) {
                                    inFormOpts[0].click();
                                    return 'no-optional:' + inFormOpts[0].textContent.trim().substring(0, 40);
                                }

                                // strategy C: li要素（April などの date picker 対応）
                                const lis = [...document.querySelectorAll('li')]
                                    .filter(el => {
                                        if (!isVis(el)) return false;
                                        return !isBad(el.textContent.trim());
                                    });
                                if (lis.length > 0) {
                                    lis[0].click();
                                    return 'li:' + lis[0].textContent.trim().substring(0, 40);
                                }

                                // strategy D: eds-option-add (自由入力型ドロップダウン)
                                // "Add a new item" ボタンをクリックして自由入力フィールドを開く
                                const addBtn = [...document.querySelectorAll(
                                    '[class*="eds-option-add"], [class*="option-add"], [class*="option__add"]'
                                )].find(el => isVis(el));
                                if (addBtn) {
                                    addBtn.click();
                                    return 'add-new:clicked';
                                }

                                // Debug: 可視の[class*="option"]要素を全てリスト（form-item内外含む）
                                const allVisOpts = [...document.querySelectorAll('[class*="option"]')]
                                    .filter(el => isVis(el))
                                    .slice(0, 5)
                                    .map(el => {
                                        const inForm = !!el.closest('[class*="form-item"]');
                                        const r = el.getBoundingClientRect();
                                        return `${el.className.slice(0,20)}|inForm=${inForm}|top=${Math.round(r.top)}|"${el.textContent.trim().slice(0,15)}"`;
                                    });
                                return 'debug:' + JSON.stringify(allVisOpts);
                            }
                        """)
                        if _selected and str(_selected) == 'add-new:clicked':
                            # strategy D 成功: クリック後にフォーカスが自由入力フィールドに移る
                            # → そのままタイプしてEnterで確定
                            _human_wait(0.5, 0.8)
                            _add_val = "Plastic"
                            try:
                                self._page.keyboard.type(_add_val, delay=40)
                                _human_wait(0.3, 0.5)
                                self._page.keyboard.press("Enter")
                                logger.info(f"  Spec dropdown '{_next}' → 'add-new:{_add_val}'")
                            except Exception:
                                self._page.keyboard.press("Escape")
                        elif _selected and not str(_selected).startswith('debug:'):
                            logger.info(f"  Spec dropdown '{_next}' → '{str(_selected)[:40]}'")
                        elif _selected and str(_selected).startswith('debug:'):
                            _dbg = _selected[6:200]
                            logger.warning(f"  Spec dropdown '{_next}': オプションなし — DEBUG={_dbg[:120]}")
                            # "No Result" の場合はロード待ちの可能性 → 3秒待ってリトライ
                            if 'No Result' in _dbg or 'Loading' in _dbg:
                                _human_wait(2.5, 3.5)
                                _retry_sel = self._page.evaluate("""
                                    () => {
                                        const isVis = (el) => {
                                            const r = el.getBoundingClientRect();
                                            return r.width > 0 && r.height > 0
                                                && r.top >= 0 && r.top < window.innerHeight;
                                        };
                                        const isBad = (txt) => {
                                            if (!txt || txt.length === 0 || txt.length > 100) return true;
                                            if (txt.includes('Add a new') || txt.includes('Loading')
                                                || txt.includes('No Result') || txt.includes('Recommended Value')
                                                || txt.includes('Please') || /^[\\s\\n]+$/.test(txt)) return true;
                                            return false;
                                        };
                                        const opts = [...document.querySelectorAll('[class*="option"]')]
                                            .filter(el => isVis(el) && !isBad(el.textContent.trim())
                                                && !el.closest('[class*="form-item"]'));
                                        if (opts.length > 0) {
                                            opts[0].click();
                                            return 'retry:' + opts[0].textContent.trim().slice(0, 40);
                                        }
                                        return null;
                                    }
                                """)
                                if _retry_sel:
                                    logger.info(f"  Spec dropdown '{_next}' → '{_retry_sel}'")
                                else:
                                    self._page.keyboard.press("Escape")
                                    logger.info(f"  Spec dropdown '{_next}': リトライも失敗 → スキップ")
                            else:
                                self._page.keyboard.press("Escape")
                        else:
                            self._page.keyboard.press("Escape")
                            logger.info(f"  Spec dropdown '{_next}': オプションなし → スキップ")
                        _human_wait(0.3, 0.5)
            except Exception as e:
                logger.warning(f"  Specificationタブエラー（続行）: {e}")

            # ── Spec テキスト再入力（ドロップダウン選択後に Vue.js が TIS フィールドをクリアする対策） ──
            # Spec dropdown loop の後に再度スクロールして空の input を全て埋める
            # (TIS No. / TIS Certificate No. / TIS license website / Input Voltage など)
            try:
                self._page.evaluate("window.scrollTo(0, 0)")
                _human_wait(0.5, 0.8)
                refill_count = self._page.evaluate("""
                    () => {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value').set;
                        const inputs = [...document.querySelectorAll(
                            'input.eds-input__input, input[type="number"], input[type="text"], ' +
                            'input:not([type])[class]'
                        )].filter(i => i.offsetParent !== null && !i.readOnly && !i.disabled
                                    && i.type !== 'radio' && i.type !== 'checkbox'
                                    && !i.value);
                        const getLabel = (inp) => {
                            let el = inp.parentElement;
                            for (let i = 0; i < 8; i++) {
                                if (!el) break;
                                const lbl = el.querySelector(
                                    'label, [class*="label"], [class*="form-item__label"]'
                                );
                                if (lbl && lbl !== inp) return lbl.textContent.toLowerCase();
                                el = el.parentElement;
                            }
                            return '';
                        };
                        let filled = 0;
                        for (const inp of inputs) {
                            const ph  = (inp.placeholder || '').toLowerCase();
                            const lbl = getLabel(inp);
                            // 危険なフィールドをスキップ（URL/認定/ブランド/ライセンス系は無視）
                            // URLフィールドを誤った値で埋めるとサーバーバリデーションに失敗する
                            if (ph.includes('brand') || ph.includes('product') ||
                                    ph.includes('name') || ph.includes('color') ||
                                    ph.includes('e.g. color') || ph.includes('e.g. red') ||
                                    ph.includes('sku') ||
                                    ph.includes('http') || ph.includes('url') ||
                                    ph.includes('website') || ph.includes('site') ||
                                    lbl.includes('brand') || lbl.includes('license') ||
                                    lbl.includes('parent') || lbl.includes('sku') ||
                                    lbl.includes('tis') || lbl.includes('website') ||
                                    lbl.includes('url') || lbl.includes('certificate') ||
                                    lbl.includes('certification')) continue;
                            let val;
                            if (ph.includes('no.') || ph.includes('number') ||
                                       ph.includes('certificate') || ph.includes('#') ||
                                       ph.includes('code') || ph.includes('id') ||
                                       ph.includes('tis') ||
                                       lbl.includes('tis') || lbl.includes('certificate') ||
                                       lbl.includes('no.') || lbl.includes('fda') ||
                                       inp.type === 'number') {
                                val = '12345';
                            } else {
                                val = '3D Printed Plastic';
                            }
                            setter.call(inp, val);
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            inp.dispatchEvent(new InputEvent('input', {
                                bubbles: true, data: val, inputType: 'insertText'}));
                            inp.blur();
                            filled++;
                            if (filled >= 25) break;
                        }
                        return filled;
                    }
                """)
                if refill_count > 0:
                    logger.info(f"  Spec再入力: {refill_count} 件（ドロップダウン選択後の再埋め）")
            except Exception as _re:
                logger.debug(f"  Spec再入力エラー（無視）: {_re}")

            # ── Brand 再アサーション（Spec再入力後に実行 — Brand EDS内部inputリセット対策） ──
            # Spec再入力が Brand EDS コンボボックス内部の空 input を誤って埋めた場合に備えて、
            # Spec再入力の後で Brand を "No Brand" に再確認する。
            try:
                _human_wait(0.3, 0.5)
                self._page.evaluate("window.scrollTo(0, 0)")
                _bra_state = self._page.evaluate("""
                    () => {
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walker.nextNode())) {
                            const clean = node.textContent.trim().replace(/[*\\s]/g,'');
                            if (clean !== 'Brand') continue;
                            const par = node.parentElement;
                            if (!par || par.offsetParent === null) continue;
                            let c = par;
                            for (let i = 0; i < 12; i++) {
                                if (!c) break;
                                const sel = c.querySelector('[class*="eds-selector"]');
                                if (sel && sel.offsetParent !== null) {
                                    return {found: true, txt: sel.textContent.trim()};
                                }
                                c = c.parentElement;
                            }
                        }
                        return {found: false};
                    }
                """)
                _bra_txt = _bra_state.get('txt', '')
                _bra_ok = _bra_txt in ('No Brand', 'ไม่มีแบรนด์')
                logger.info(f"  Brand再アサーション(Spec再入力後): txt='{_bra_txt}', ok={_bra_ok}")
                if _bra_state.get('found') and not _bra_ok:
                    # Brand が No Brand 以外 → クリックして No Brand を再選択
                    _bra_clicked = self._page.evaluate("""
                        () => {
                            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                            let node;
                            while ((node = walker.nextNode())) {
                                if (node.textContent.trim().replace(/[*\\s]/g,'') !== 'Brand') continue;
                                const par = node.parentElement;
                                if (!par || par.offsetParent === null) continue;
                                let c = par;
                                for (let i = 0; i < 12; i++) {
                                    if (!c) break;
                                    const sel = c.querySelector('[class*="eds-selector"]');
                                    if (sel && sel.offsetParent !== null) {
                                        sel.scrollIntoView({block:'center'});
                                        sel.click();
                                        return true;
                                    }
                                    c = c.parentElement;
                                }
                            }
                            return false;
                        }
                    """)
                    if _bra_clicked:
                        _human_wait(0.8, 1.2)
                        _bra_done = False
                        for _bra_sel in [
                            'li:has-text("No Brand")',
                            '[role="option"]:has-text("No Brand")',
                            '[class*="option"]:has-text("No Brand")',
                        ]:
                            try:
                                _bra_opt = self._page.locator(_bra_sel).first
                                if _bra_opt.count() and _bra_opt.is_visible():
                                    _bra_opt.click()
                                    logger.info(f"  Brand再アサーション: No Brand 再選択完了 (via {_bra_sel})")
                                    _bra_done = True
                                    _human_wait(0.5, 0.8)
                                    break
                            except Exception:
                                pass
                        if not _bra_done:
                            self._page.keyboard.press("Escape")
                            logger.warning("  Brand再アサーション: No Brand 選択失敗 → Escape")
            except Exception as _bra_e:
                logger.debug(f"  Brand再アサーションエラー（続行）: {_bra_e}")

            _human_wait(0.8, 1.2)

            # ── Description タブ ──────────────────────
            try:
                desc_tab = self._page.get_by_text("Description", exact=True).first
                if desc_tab.count():
                    desc_tab.click(timeout=10000)
                    _human_wait(1.5, 2.5)
                    desc_el = self._page.locator('div[contenteditable="true"], .ql-editor').first
                    if desc_el.count():
                        # JS経由でQuillエディタにコンテンツをセット（click hangup回避）
                        safe_desc = description[:800].replace('\\', '\\\\').replace('`', '\\`').replace('$', '\\$')
                        desc_set = self._page.evaluate(f"""
                            () => {{
                                const editor = document.querySelector('.ql-editor');
                                if (!editor) return false;
                                const container = editor.closest('[class*="ql-container"]') || editor.parentElement;
                                // Quill APIでセット
                                if (container && container.__quill) {{
                                    container.__quill.setText(`{safe_desc}`);
                                    return 'quill-api';
                                }}
                                // フォールバック: innerHTML + input イベント
                                editor.focus();
                                document.execCommand('selectAll', false, null);
                                document.execCommand('insertText', false, `{safe_desc}`);
                                return 'execCommand';
                            }}
                        """)
                        if desc_set:
                            logger.info(f"  説明入力完了 ({desc_set})")
                        else:
                            # キーボード入力フォールバック
                            try:
                                desc_el.click(timeout=3000, force=True)
                            except Exception:
                                pass
                            _human_wait(0.2, 0.3)
                            self._page.keyboard.press("Control+A")
                            self._page.keyboard.type(description[:500], delay=5)
                            logger.info("  説明入力完了 (keyboard)")
            except Exception as e:
                logger.warning(f"説明タブエラー（続行）: {e}")

            _human_wait(0.8, 1.2)

            # ── Sales Information タブ（価格・在庫）────
            try:
                sales_tab = self._page.get_by_text("Sales Information", exact=True).first
                if sales_tab.count():
                    sales_tab.click()
                    _human_wait(2, 3)

                    price_filled = False
                    stock_filled = False

                    # ── 診断スクリーンショット（Sales Info直後）──────────
                    self._screenshot(f"debug_{mw_id}_salesinfo_before")

                    # 状態確認（textContent で hidden 要素も含めてチェック）
                    var_state = self._page.evaluate("""
                        () => {
                            const tc = document.body.textContent;
                            const switches = [...document.querySelectorAll('input[type="checkbox"]')];
                            const checkedCount = switches.filter(s => s.checked).length;
                            return {
                                hasVarList: tc.includes('Variation List'),
                                hasVar1: tc.includes('Variation 1'),
                                hasEnableVar: tc.includes('Enable Variations'),
                                checkedSwitches: checkedCount,
                                visibleInputs: [...document.querySelectorAll('input.eds-input__input')]
                                    .filter(i => i.offsetParent !== null).length
                            };
                        }
                    """)
                    logger.info(f"  [診断] Variation状態={var_state}")

                    # バリエーション不要 → 通常の価格・在庫入力（3Dプリント品はバリエーション不使用）
                    logger.info("  通常価格入力（バリエーションなし）")

                    # 価格入力: L1 text が '฿' の input を特定
                    try:
                        price_marked = self._page.evaluate("""
                            () => {
                                document.querySelectorAll('[data-bot-price]').forEach(e => e.removeAttribute('data-bot-price'));
                                const inputs = [...document.querySelectorAll('input.eds-input__input')]
                                    .filter(i => i.offsetParent !== null);
                                for (const inp of inputs) {
                                    const L1 = inp.parentElement?.parentElement;
                                    if (L1 && L1.textContent.trim() === '\u0e3f') {
                                        inp.setAttribute('data-bot-price', 'true');
                                        inp.scrollIntoView({block: 'center'});
                                        return true;
                                    }
                                }
                                return false;
                            }
                        """)
                        if price_marked:
                            price_inp = self._page.locator('[data-bot-price="true"]').first
                            price_inp.click(click_count=3)
                            _human_wait(0.1, 0.2)
                            self._page.keyboard.type(str(int(price)), delay=50)
                            _human_wait(0.5, 1.0)
                            self._page.evaluate("""
                                () => {
                                    const el = document.activeElement;
                                    if (el) {
                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                                        el.blur();
                                    }
                                }
                            """)
                            _human_wait(0.3, 0.5)
                            got_val = price_inp.input_value()
                            logger.info(f"  価格入力完了: {int(price)} THB (確認={got_val})")
                            price_filled = True
                        else:
                            logger.warning("  ⚠️ 価格inputが特定できません")
                    except Exception as e:
                        logger.warning(f"  価格入力エラー: {e}")

                    # 在庫入力: Stockラベルの親コンテナから input を特定（SKU/ParentSKU との混同を避ける）
                    _human_wait(0.5, 1.0)
                    try:
                        stock_marked = self._page.evaluate("""
                            () => {
                                document.querySelectorAll('[data-bot-stock]').forEach(e => e.removeAttribute('data-bot-stock'));
                                // "* Stock" ラベルを持つフォームアイテムの input を探す
                                const labels = [...document.querySelectorAll(
                                    'label, .eds-form-item__label, [class*="form-item__label"]'
                                )];
                                const stockLabel = labels.find(l => {
                                    const t = l.textContent.replace('*','').trim();
                                    return t === 'Stock' || t === 'จำนวนสินค้า';
                                });
                                if (stockLabel) {
                                    let container = stockLabel.parentElement;
                                    for (let i = 0; i < 6; i++) {
                                        if (!container) break;
                                        const inp = container.querySelector('input');
                                        if (inp && inp.offsetParent !== null && !inp.readOnly && !inp.disabled) {
                                            inp.setAttribute('data-bot-stock', 'true');
                                            inp.scrollIntoView({block: 'center'});
                                            return {found: true, method: 'label', val: inp.value};
                                        }
                                        container = container.parentElement;
                                    }
                                }
                                // フォールバック: 価格inputの次の可視inputでSKUでないもの
                                const priceInp = document.querySelector('[data-bot-price]');
                                if (priceInp) {
                                    const allInputs = [...document.querySelectorAll('input.eds-input__input')]
                                        .filter(i => i.offsetParent !== null && !i.readOnly);
                                    const priceIdx = allInputs.indexOf(priceInp);
                                    for (let j = priceIdx + 1; j < allInputs.length; j++) {
                                        const inp = allInputs[j];
                                        const anc = inp.closest('[class*="form-item"]');
                                        const ancText = anc ? anc.textContent : '';
                                        if (!ancText.includes('SKU') && !ancText.includes('Wholesale')) {
                                            inp.setAttribute('data-bot-stock', 'true');
                                            inp.scrollIntoView({block: 'center'});
                                            return {found: true, method: 'next_after_price', val: inp.value};
                                        }
                                    }
                                }
                                return {found: false};
                            }
                        """)
                        logger.info(f"  在庫input特定: {stock_marked}")
                        if stock_marked.get("found"):
                            stock_inp = self._page.locator('[data-bot-stock="true"]').first
                            stock_inp.click(click_count=3)
                            _human_wait(0.1, 0.2)
                            self._page.keyboard.type(str(stock), delay=50)
                            self._page.evaluate("""
                                () => {
                                    const el = document.activeElement;
                                    if (el) {
                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                        el.dispatchEvent(new Event('blur',   {bubbles: true}));
                                        el.blur();
                                    }
                                }
                            """)
                            _human_wait(0.3, 0.5)
                            actual = stock_inp.input_value()
                            logger.info(f"  在庫入力完了: {stock} (確認={actual})")
                            stock_filled = True
                        else:
                            logger.warning("  ⚠️ 在庫inputが特定できません")
                    except Exception as e:
                        logger.warning(f"  在庫入力エラー: {e}")

            except Exception as e:
                logger.warning(f"Sales Informationタブエラー（続行）: {e}")

            _human_wait(0.8, 1.2)

            # ── Shipping タブ（重量・配送設定）──────────
            # デバッグで確認済み:
            # - 重量input: class="eds-input__input" inside ".price-input" with parent text="kg"
            # - Apply & Enable Channel ボタン: DOMには常にあるが非表示(hidden)のため force=True 必要
            try:
                # チャットパネルを閉じてからタブクリック（遮断防止）
                self._dismiss_chat_panel()
                shipping_tab = self._page.get_by_text("Shipping", exact=True).first
                if shipping_tab.count():
                    try:
                        shipping_tab.click(timeout=10000)
                    except Exception:
                        # タイムアウト時はJS直接クリック
                        self._page.evaluate("""
                            () => {
                                const tabs = [...document.querySelectorAll('[class*="tabs__nav-tab"], [class*="tab-item"]')];
                                const t = tabs.find(el => el.textContent.trim() === 'Shipping');
                                if (t) t.click();
                            }
                        """)
                        logger.info("  Shippingタブ: JS直接クリック（タイムアウト回避）")
                    _human_wait(2, 3)

                    # 重量入力: .price-input コンテナの中で parent text="kg" のもの
                    weight_filled = False
                    try:
                        weight_inp = None
                        try:
                            w = self._page.locator('.price-input').filter(
                                has_text="kg"
                            ).locator('input.eds-input__input').first
                            w.wait_for(state="visible", timeout=8000)
                            weight_inp = w
                        except Exception:
                            # フォールバック: * Weight ラベル近傍のinputを探す
                            marked = self._page.evaluate("""
                                () => {
                                    document.querySelectorAll('[data-bot-weight]').forEach(e => e.removeAttribute('data-bot-weight'));
                                    const allInputs = [...document.querySelectorAll('input.eds-input__input')]
                                        .filter(i => i.offsetParent !== null && !i.readOnly);
                                    for (const inp of allInputs) {
                                        let el = inp;
                                        for (let i = 0; i < 5; i++) {
                                            el = el.parentElement;
                                            if (!el) break;
                                            if (el.textContent.includes('kg') && el.textContent.includes('Weight')) {
                                                inp.setAttribute('data-bot-weight', 'true');
                                                return true;
                                            }
                                        }
                                    }
                                    return false;
                                }
                            """)
                            if marked:
                                weight_inp = self._page.locator('[data-bot-weight="true"]').first
                        if weight_inp and weight_inp.count():
                            weight_inp.scroll_into_view_if_needed()
                            weight_inp.click(click_count=3)
                            _human_wait(0.2, 0.3)
                            weight_inp.fill(f"{weight_kg:.2f}")
                            self._page.keyboard.press("Tab")
                            logger.info(f"  重量入力完了: {weight_kg:.2f} kg")
                            weight_filled = True
                        else:
                            logger.warning("  ⚠️ 重量inputが見つかりません")
                    except Exception as e:
                        logger.warning(f"  重量入力エラー: {e}")

                    # 配送オプション確認
                    # アクティブな配送トグルがあれば設定済み、なければ Enable をクリック
                    # 注意: 'button:has-text("Enable")' は viewport 外の '+ Enable Variations'
                    #       ボタンにも一致してしまう。JS でテキスト完全一致 + viewport 内に限定する。
                    _human_wait(1, 1.5)
                    has_active = self._page.evaluate("""
                        () => {
                            // eds-toggle--checked や checked クラスを持つトグルを探す
                            const toggles = [...document.querySelectorAll(
                                '[class*="toggle"][class*="check"], [class*="toggle"][class*="activ"], ' +
                                '[class*="switch"][class*="check"], input[type="checkbox"]:checked'
                            )];
                            return toggles.filter(t => t.offsetParent !== null).length > 0;
                        }
                    """)
                    if has_active:
                        logger.info("  配送: トグル有効を確認 — Enable不要")
                    else:
                        # JS でテキスト完全一致 ('Enable' のみ) かつ viewport 内のボタンを探す
                        # → 'Enable Variations' ボタンは viewport 外なので除外される
                        enable_clicked = self._page.evaluate("""
                            () => {
                                const btns = [...document.querySelectorAll('button')];
                                const vh = window.innerHeight;
                                for (const btn of btns) {
                                    const txt = btn.textContent.trim();
                                    if (txt !== 'Enable') continue;
                                    const r = btn.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0 && r.top >= 0 && r.top < vh) {
                                        btn.click();
                                        return true;
                                    }
                                }
                                return false;
                            }
                        """)
                        if enable_clicked:
                            logger.info("  配送: Enable クリック → ダイアログ待ち")
                            _human_wait(3, 5)
                            # ダイアログ内の確認ボタンを探す（多様なテキスト・非表示ボタンも対象）
                            applied = self._page.evaluate("""
                                () => {
                                    const candidates = [
                                        'Apply & Enable Channel', 'Apply and Enable Channel',
                                        'Apply', 'Confirm', 'OK', 'ยืนยัน', 'ตกลง',
                                        'สมัครและเปิดใช้', 'เปิดใช้งาน'
                                    ];
                                    const btns = [...document.querySelectorAll('button, [role="button"]')];
                                    for (const text of candidates) {
                                        const btn = btns.find(b => b.textContent.trim().includes(text));
                                        if (btn) { btn.click(); return text; }
                                    }
                                    // ダイアログ内のCancel以外のボタン
                                    const dialog = document.querySelector(
                                        '[role="dialog"], [class*="modal"], [class*="dialog"], [class*="overlay"]'
                                    );
                                    if (dialog) {
                                        const b = [...dialog.querySelectorAll('button')].find(b =>
                                            !b.textContent.includes('Cancel') && !b.textContent.includes('ยกเลิก')
                                        );
                                        if (b) { b.click(); return 'dialog-btn:' + b.textContent.trim(); }
                                    }
                                    return null;
                                }
                            """)
                            if applied:
                                logger.info(f"  配送: ダイアログ確認ボタンクリック ({applied})")
                                _human_wait(2, 3)
                            else:
                                logger.info("  配送: ダイアログボタン未発見 → Escape")
                                self._page.keyboard.press("Escape")
                                _human_wait(1, 1.5)
                        else:
                            logger.info("  配送: Enable不要（ボタン非表示）")

            except Exception as e:
                logger.warning(f"Shippingタブエラー（続行）: {e}")

            # ── Pre-Order設定（No — 即出荷で必須フィールドを最小化） ────────
            try:
                pre_order_no = self._page.locator('label:has-text("No")').filter(
                    has=self._page.locator('input[type="radio"]')
                ).first
                if not (pre_order_no.count() and pre_order_no.is_visible()):
                    pre_order_no = self._page.locator('input[type="radio"][value="false"], input[type="radio"]:first-of-type').first
                if pre_order_no.count() and pre_order_no.is_visible():
                    pre_order_no.click()
                    _human_wait(0.5, 1.0)
                    logger.info("  Pre-Order: No に設定")
                else:
                    logger.info("  Pre-Order: ラジオボタン未検出（デフォルト使用）")
            except Exception as e:
                logger.warning(f"  Pre-Order設定エラー（続行）: {e}")

            _human_wait(0.8, 1.2)

            # ── Variation誤有効化ガード ──────────────────────────
            # シッピングEnableボタンのクリック等でVariation1が誤って有効化されている場合
            # Sales Informationタブに戻って確認・無効化する
            try:
                var_was_active = self._page.evaluate("""
                    () => {
                        const tc = document.body.textContent;
                        // 'Variation1' または 'Variation 1' が存在し、かつ
                        // 可視のvariation関連inputがあれば有効化とみなす
                        if (!tc.includes('Variation1') && !tc.includes('Variation 1')) return false;
                        const varInputs = [...document.querySelectorAll('input')]
                            .filter(inp => {
                                if (inp.offsetParent === null) return false;
                                const ph = (inp.placeholder || '').toLowerCase();
                                const par = (inp.parentElement?.textContent || '').toLowerCase();
                                return ph.includes('color') || ph.includes('variation') ||
                                       par.includes('variation1') || par.includes('variation 1') ||
                                       ph.includes('e.g. color') || ph.includes('e.g. red');
                            });
                        return varInputs.length > 0;
                    }
                """)
                if var_was_active:
                    logger.warning("  ⚠️ Variation1が誤って有効化されています — 無効化します")
                    # Sales Informationタブに移動して無効化
                    _si_tab = self._page.get_by_text("Sales Information", exact=True).first
                    if _si_tab.count():
                        _si_tab.click()
                        _human_wait(1, 1.5)
                    self._deactivate_variations()
                    _human_wait(1, 1.5)
                else:
                    logger.info("  Variation状態OK（有効化なし）")
            except Exception as _ve:
                logger.warning(f"  Variation確認エラー（続行）: {_ve}")

            _random_scroll(self._page)

            # ── オーバーレイ/ダイアログをクリアしてからPublish ──
            try:
                # チャットパネルや開いたままのダイアログをEscapeで閉じる
                self._page.keyboard.press("Escape")
                _human_wait(0.3, 0.5)
                # Shopeeチャットパネルが開いていれば閉じる
                self._page.evaluate("""
                    () => {
                        // チャットサイドパネルを閉じるボタンを探す
                        const closeBtns = [...document.querySelectorAll('button, [role="button"]')]
                            .filter(el => {
                                const txt = el.textContent.trim();
                                const cls = (el.className || '').toString();
                                return cls.includes('chat') || cls.includes('close') || txt === '×' || txt === '✕';
                            });
                        for (const btn of closeBtns) {
                            const rect = btn.getBoundingClientRect();
                            // 右上エリアのclose buttonのみクリック
                            if (rect.right > window.innerWidth * 0.7 && rect.top < 200) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                _human_wait(0.3, 0.5)
            except Exception:
                pass

            # ── Save & Publish ────────────────────────────
            # チャットパネルが開いている場合はボタンクリックを遮るため閉じる
            self._dismiss_chat_panel()
            _human_wait(0.3, 0.5)
            published_url = self._click_publish(mw_id)
            if published_url:
                count = _increment_today_count()
                logger.info(f"✅ 出品成功 ({count}/{BROWSER_SETTINGS['daily_limit']}件): {published_url}")
                return published_url
            else:
                return None

        except Exception as e:
            logger.error(f"❌ 出品エラー ({mw_id}): {e}")
            self._screenshot(f"error_{mw_id}_unexpected")
            return None

    def _dismiss_chat_panel(self):
        """
        Shopee セラーセンターのチャットサイドパネル（sidebar-panel with-shadow）を閉じる。
        このパネルはタブクリックや Save & Publish ボタンのクリックを遮ることがある。
        """
        try:
            dismissed = self._page.evaluate("""
                () => {
                    // チャットパネルを探す
                    const panel = document.querySelector(
                        '.sidebar-panel.with-shadow, [class*="sidebar-panel"][class*="shadow"], ' +
                        '[class*="chat-panel"], [class*="chat-sidebar"]'
                    );
                    if (!panel || panel.offsetParent === null) return 'none';
                    // 閉じるボタンを探す
                    const closeBtn = panel.querySelector(
                        '[class*="close"], [aria-label*="close"], [aria-label*="Close"], button'
                    );
                    if (closeBtn) {
                        closeBtn.click();
                        return 'closed-btn';
                    }
                    // 閉じるボタンがなければ非表示にする
                    panel.style.display = 'none';
                    panel.style.visibility = 'hidden';
                    panel.style.pointerEvents = 'none';
                    return 'hidden';
                }
            """)
            if dismissed and dismissed != 'none':
                logger.info(f"  チャットパネル非表示: {dismissed}")
                _human_wait(0.3, 0.5)
        except Exception as e:
            logger.debug(f"  チャットパネル閉じエラー（無視）: {e}")

    def _deactivate_variations(self):
        """
        Sales Information タブで Variations が誤って有効化されている場合に無効化する。
        戦略:
        1. Variation1 入力欄の近傍にある × / close ボタンをJS でクリック
        2. Variation関連のCSS class が付いた delete/remove ボタンを試す
        3. 最後の手段: Variation入力をクリアしてバリデーションエラーを軽減
        """
        try:
            # 戦略1: JS で placeholder='e.g. Color, etc' 付近の close/delete ボタンを探す
            closed = self._page.evaluate("""
                () => {
                    // Variation1 type input を見つける (placeholder: "e.g. Color, etc")
                    const varInput = [...document.querySelectorAll('input')]
                        .find(i => {
                            if (i.offsetParent === null) return false;
                            const ph = (i.placeholder || '').toLowerCase();
                            return ph.includes('color') || ph.includes('variation') ||
                                   ph.includes('e.g. color');
                        });
                    if (!varInput) return 'no-input';

                    // 親要素を辿って close/delete ボタンを探す
                    let container = varInput;
                    for (let i = 0; i < 6; i++) {
                        container = container.parentElement;
                        if (!container) break;
                        const btns = [...container.querySelectorAll(
                            'button, [role="button"], svg, [class*="close"], [class*="delete"], [class*="remove"]'
                        )].filter(el => el.offsetParent !== null && el !== varInput);
                        if (btns.length > 0) {
                            btns[0].click();
                            return 'clicked:' + (btns[0].className || btns[0].tagName);
                        }
                    }
                    return 'no-btn';
                }
            """)
            if closed and closed.startswith('clicked:'):
                logger.info(f"  Variation削除(JS close): {closed}")
                _human_wait(0.8, 1.2)
                return

            # 戦略2: CSS class ベースの削除ボタン候補
            del_selectors = [
                '[class*="variation"] [class*="delete"]',
                '[class*="variation"] [class*="remove"]',
                '[class*="variation"] [class*="close"]',
                '[class*="var-name"] ~ button',
                '[class*="variation-item"] button',
                '[class*="var-item"] button',
            ]
            for sel in del_selectors:
                try:
                    btns = self._page.locator(sel).all()
                    for btn in btns:
                        if btn.is_visible():
                            btn.click(force=True)
                            _human_wait(0.5, 1.0)
                            logger.info(f"  Variation削除(CSS): {sel}")
                            return
                except Exception:
                    pass

            # 戦略3: Variation入力をクリアしてフォームエラーを軽減
            cleared = self._page.evaluate("""
                () => {
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value'
                    ).set;
                    let count = 0;
                    [...document.querySelectorAll('input')].forEach(inp => {
                        if (inp.offsetParent === null) return;
                        const ph = (inp.placeholder || '').toLowerCase();
                        if (ph.includes('color') || ph.includes('variation') ||
                            ph.includes('e.g. red') || ph.includes('explanation')) {
                            setter.call(inp, '');
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            count++;
                        }
                    });
                    return count;
                }
            """)
            if cleared:
                logger.info(f"  Variation入力をJSでクリア ({cleared}件)")
            else:
                logger.warning("  Variation無効化: 対象要素が見つかりません")
        except Exception as e:
            logger.warning(f"  Variation無効化エラー（続行）: {e}")

    def _react_set_input(self, selector: str, value: str) -> bool:
        """React制御inputにnative setterでvalueを注入し、input/changeイベントを発火する。
        DOM確認後Trueを返す。対象が見つからない場合はFalse。"""
        return self._page.evaluate("""
            ([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el || el.offsetParent === null) return false;
                el.scrollIntoView({block: 'center'});
                el.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input',  {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.blur();
                return el.value === val;
            }
        """, [selector, value])

    def _fill_variation_pricing(self, price: float, stock: int) -> tuple[bool, bool]:
        """
        Enable Variations クリック直後の状態から:
        1. Variation 1 名を入力（placeholder="e.g. Color, etc"）
        2. Option を追加（placeholder="e.g. Red, etc"、Enter で確定）
        3. テーブル行の価格（placeholder="Price"）・在庫（placeholder="Stock"）を入力
        Returns (price_filled, stock_filled)
        診断確認済みプレースホルダー（2026-04 Shopee seller.shopee.co.th）:
          idx4 "e.g. Color, etc" = Variation 1 type name
          idx5 "e.g. Red, etc"   = Option value
          idx7 "Price" grandparent='฿' = row price
          idx8 "Stock"           = row stock
        """
        price_filled = False
        stock_filled = False

        try:
            _human_wait(1.0, 1.5)

            # ── "Got it" ツールチップを閉じる ───────────────────────
            # Enable Variations 直後に "Select a standard variation name..." ツールチップが出る
            # このツールチップの Got it ボタンが Tab フォーカスを横取りするため先に閉じる
            try:
                self._page.evaluate("""
                    () => {
                        [...document.querySelectorAll('button')].forEach(btn => {
                            if (btn.textContent.trim() === 'Got it') btn.click();
                        });
                    }
                """)
                _human_wait(0.5, 0.8)
            except Exception:
                pass

            # ── 診断: 可視 input 一覧（最大 12 件） ──────────────
            inputs_info = self._page.evaluate("""
                () => {
                    const inputs = [...document.querySelectorAll('input')]
                        .filter(i => i.offsetParent !== null && !i.readOnly && !i.disabled);
                    return inputs.slice(0, 12).map((inp, idx) => ({
                        idx, ph: inp.placeholder, val: inp.value.substring(0, 20),
                        gp: (inp.parentElement?.parentElement?.textContent || '').trim().substring(0, 20)
                    }));
                }
            """)
            logger.info(f"  [Var診断] inputs: {inputs_info}")

            # ── Step 1+2: Variation 1 名 → Options 一括入力 ─────
            # Tab 後は Options input にフォーカスが移るので keyboard.type() で直接入力する
            # （placeholder が変わっても focus は移動しないのでセレクタ不要）
            var_name_filled = False
            option_filled = False
            try:
                vn_inp = self._page.locator('input[placeholder="e.g. Color, etc"]').first
                if vn_inp.count() and vn_inp.is_visible():
                    vn_inp.scroll_into_view_if_needed()
                    vn_inp.click()
                    _human_wait(0.2, 0.3)
                    vn_inp.fill("ขนาด")
                    # Tab でフォーカスを Options input (idx 5) へ移す
                    self._page.keyboard.press("Tab")
                    var_name_filled = True
                    logger.info("  Variation名入力: ขนาด → Tab → Options へ")
                    # ── Step 2 (インライン): Tab 直後に keyboard.type で Options に入力 ──
                    _human_wait(0.2, 0.3)
                    self._page.keyboard.type("มาตรฐาน")
                    _human_wait(0.2, 0.3)
                    self._page.keyboard.press("Enter")
                    option_filled = True
                    logger.info("  Option入力: มาตรฐาน (keyboard.type after Tab)")
                    _human_wait(1.0, 1.5)
            except Exception as e:
                logger.warning(f"  Variation名/Option入力エラー: {e}")

            # ── フォールバック: JS native setter ──────────────────
            if not var_name_filled:
                logger.warning("  ⚠️ Variation名入力欄なし — JS fallback")
                fill_js = self._page.evaluate("""
                    () => {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value').set;
                        const inputs = [...document.querySelectorAll('input.eds-input__input')]
                            .filter(i => i.offsetParent !== null && !i.readOnly);
                        // タイトル欄 (placeholder に "Colors" が含まれる) を誤マッチしないよう
                        // 短いプレースホルダのみ対象にする (タイトル欄は 50 文字超)
                        const varInputs = inputs.filter(i => i.placeholder.length < 50);
                        const nameInp = varInputs.find(i =>
                            i.placeholder === 'Type or Select' ||
                            i.placeholder.includes('e.g. Color') ||
                            i.placeholder.includes('Color, etc')
                        );
                        const optInp  = inputs.find(i => i.placeholder.includes('Red') || i.placeholder.includes('e.g. Red'));
                        const res = {nameFound: false, optFound: false};
                        if (nameInp) {
                            nameInp.click();
                            setter.call(nameInp, '\u0e02\u0e19\u0e32\u0e14');
                            nameInp.dispatchEvent(new Event('input',  {bubbles: true}));
                            nameInp.dispatchEvent(new Event('change', {bubbles: true}));
                            res.nameFound = true;
                        }
                        if (optInp) {
                            optInp.click();
                            setter.call(optInp, '\u0e21\u0e32\u0e15\u0e23\u0e10\u0e32\u0e19');
                            optInp.dispatchEvent(new Event('input',  {bubbles: true}));
                            optInp.dispatchEvent(new Event('change', {bubbles: true}));
                            optInp.dispatchEvent(new KeyboardEvent('keydown',
                                {key: 'Enter', keyCode: 13, bubbles: true}));
                            res.optFound = true;
                        }
                        return res;
                    }
                """)
                logger.info(f"  Variation JS fallback: {fill_js}")
                if fill_js.get("nameFound"):
                    var_name_filled = True
                if fill_js.get("optFound"):
                    option_filled = True
            elif not option_filled:
                # Variation 名は入ったが Options が未確定 → セレクタで再試行
                logger.warning("  ⚠️ Option 未入力 — セレクタで再試行")
                try:
                    opt2 = self._page.locator('input[placeholder="e.g. Red, etc"]').first
                    if opt2.count() and opt2.is_visible():
                        opt2.click()
                        _human_wait(0.2, 0.3)
                        opt2.fill("มาตรฐาน")
                        self._page.keyboard.press("Enter")
                        option_filled = True
                        logger.info("  Option再入力: มาตรฐาน")
                        _human_wait(1.0, 1.5)
                except Exception as e2:
                    logger.warning(f"  Option再入力エラー: {e2}")

            _human_wait(1.5, 2.0)

            # 状態診断: Options が入ったか確認
            opt_state = self._page.evaluate("""
                () => {
                    const tc = document.body.textContent;
                    const inputs = [...document.querySelectorAll('input')]
                        .filter(i => i.offsetParent !== null && !i.readOnly);
                    return {
                        hasMat: tc.includes('\u0e21\u0e32\u0e15\u0e23\u0e10\u0e32\u0e19'),
                        hasSize: tc.includes('\u0e02\u0e19\u0e32\u0e14'),
                        visibleInputs: inputs.length,
                        priceInputFound: inputs.some(i => i.placeholder === 'Price'),
                        stockInputFound: inputs.some(i => i.placeholder === 'Stock'),
                    };
                }
            """)
            logger.info(f"  [Option後診断] {opt_state}")

            # ── Step 3 & 4: Apply To All で価格・在庫を一括入力 ──
            # placeholder="Price" は Apply To All ヘッダー行のinput（各行のpriceはplaceholder="Input"）
            # Apply To All ボタンで全バリエーション行に一括適用する
            _human_wait(0.5, 0.8)
            try:
                # ヘッダー行の価格inputに入力（React native setter）
                price_filled = self._react_set_input('input[placeholder="Price"]', str(int(price)))
                if price_filled:
                    logger.info(f"  Apply To All 価格設定: {int(price)} THB")
                else:
                    logger.warning("  Apply To All Price input が見つからない")

                # ヘッダー行の在庫inputに入力
                _human_wait(0.2, 0.3)
                stock_filled = self._react_set_input('input[placeholder="Stock"]', str(stock))
                if stock_filled:
                    logger.info(f"  Apply To All 在庫設定: {stock}")
                else:
                    logger.warning("  Apply To All Stock input が見つからない")

                # Apply To All ボタンをクリック
                _human_wait(0.3, 0.5)
                apply_btn = self._page.locator('button:has-text("Apply To All")')
                if apply_btn.count() and apply_btn.is_visible():
                    apply_btn.click()
                    _human_wait(1.0, 1.5)
                    logger.info("  Apply To All クリック完了")
                    price_filled = True
                    stock_filled = True
                else:
                    logger.warning("  Apply To All ボタンが見つからない — 行直接入力 fallback")
                    # fallback: 各行のinput[placeholder="Input"] gp='฿' に直接設定
                    set_results = self._page.evaluate("""
                        ([priceVal, stockVal]) => {
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            const inputs = [...document.querySelectorAll('input')]
                                .filter(i => i.offsetParent !== null && !i.readOnly);
                            let priceSet = false, stockSet = false;
                            for (const inp of inputs) {
                                const gp = inp.parentElement?.parentElement;
                                if (!priceSet && gp && gp.textContent.includes('\u0e3f') && inp.placeholder !== 'Price') {
                                    setter.call(inp, priceVal);
                                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                                    priceSet = true;
                                } else if (!stockSet && inp.placeholder === '' && !gp?.textContent.includes('\u0e3f') && !gp?.textContent.includes('kg')) {
                                    setter.call(inp, stockVal);
                                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                                    stockSet = true;
                                }
                                if (priceSet && stockSet) break;
                            }
                            return {priceSet, stockSet};
                        }
                    """, [str(int(price)), str(stock)])
                    price_filled = set_results.get('priceSet', False)
                    stock_filled = set_results.get('stockSet', False)
                    logger.info(f"  行直接入力 fallback: price={price_filled}, stock={stock_filled}")
            except Exception as e:
                logger.warning(f"  Variation価格・在庫入力エラー: {e}")

        except Exception as e:
            logger.error(f"  Variation入力エラー: {e}")

        return price_filled, stock_filled

    def _dismiss_listing_modals(self):
        """出品フォームに表示されるプロモーション・案内モーダルを閉じる"""
        # Shopee Standard Product ツアーポップアップ (#sspSearchTour)
        try:
            tour = self._page.locator("#sspSearchTour")
            if tour.count() and tour.is_visible():
                # ×ボタンまたはGot itボタンを探す
                for sel in ['[aria-label="Close"]', 'button:has-text("Got it")',
                            'button:has-text("Skip")', 'button:has-text("Close")']:
                    btn = tour.locator(sel).first
                    if btn.count():
                        btn.click(force=True)
                        logger.info("  sspSearchTour を閉じました")
                        _human_wait(0.5, 1.0)
                        break
                else:
                    # JavaScriptで強制非表示
                    self._page.evaluate("document.getElementById('sspSearchTour')?.remove()")
                    logger.info("  sspSearchTour をJS削除しました")
                    _human_wait(0.3, 0.5)
        except Exception:
            pass

        close_patterns = [
            # "Got it" / "Got It" は意図せず Variations 機能を有効化するため除外
            'button:has-text("OK")',
            'button:has-text("Close")',
            'button:has-text("Skip")',
            'button:has-text("Maybe Later")',
            'button:has-text("No, thanks")',
            '[aria-label="Close"]',
            '[class*="modal-close"]',
            '[class*="popup-close"]',
        ]
        for sel in close_patterns:
            try:
                btn = self._page.locator(sel).first
                if btn.count() and btn.is_visible():
                    btn.click(force=True)
                    logger.info(f"  モーダルを閉じました: {sel}")
                    _human_wait(0.3, 0.7)
            except Exception:
                pass

        # ESCでも試みる
        try:
            self._page.keyboard.press("Escape")
            _human_wait(0.3, 0.7)
        except Exception:
            pass

    def _select_category(self, mw_category: str):
        """カテゴリを選択（クリックベース）"""
        try:
            cat_btn = self._page.locator(
                'button:has-text("Category"), button:has-text("หมวดหมู่"), '
                '[class*="category"] button'
            ).first
            if not cat_btn.count():
                return
            cat_btn.click()
            _human_wait(1, 2)

            # カテゴリ名でテキスト検索
            category_text_map = {
                "Hobby & Crafts": "งานอดิเรก",
                "Home & Living": "บ้านและสวน",
                "Toys": "ของเล่น",
                "Fashion": "แฟชั่น",
                "Electronics": "อิเล็กทรอนิกส์",
                "Office": "สำนักงาน",
                "Tools": "เครื่องมือ",
                "Education": "หนังสือ",
            }
            target_text = category_text_map.get(mw_category, "งานอดิเรก")

            try:
                self._page.get_by_text(target_text, exact=False).first.click()
                _human_wait(0.5, 1.5)
                # 確定ボタン
                confirm = self._page.locator('button:has-text("Confirm"), button:has-text("ยืนยัน")').first
                if confirm.count():
                    confirm.click()
                    _human_wait(0.5, 1.0)
            except Exception:
                # カテゴリが見つからなくてもデフォルトで続行
                logger.warning(f"カテゴリ '{mw_category}' が選択できませんでした（デフォルト使用）")

        except Exception as e:
            logger.warning(f"カテゴリ選択エラー（続行）: {e}")

    def _click_publish(self, mw_id: str) -> Optional[str]:
        """Save & Publish をクリックして出品完了を確認"""
        # ── Step1 に戻っていた場合は一度だけ Step2 に復帰 ──
        # Shopee の多段フォーム。入力完了後、Vue 再レンダで Step1（画像+商品名のみ）に
        # 戻ることがある。Next Step が可視 / Save and Publish が不可視なら再クリック。
        try:
            next_btn = self._page.locator('button:has-text("Next Step"), button:has-text("ถัดไป")').first
            publish_btn = self._page.locator(
                'button:has-text("Save and Publish"), '
                'button:has-text("Save & Publish"), '
                'button:has-text("บันทึกและเผยแพร่"), '
                'button:has-text("เผยแพร่"), '
                'button:has-text("Publish")'
            ).first
            if next_btn.count() and next_btn.is_visible() and (not publish_btn.count() or not publish_btn.is_visible()):
                logger.warning("  [Pre-publish] Step1 検出 → Next Step を再クリックして Step2 に復帰")
                try:
                    self._page.evaluate("document.getElementById('sspSearchTour')?.remove()")
                except Exception:
                    pass
                try:
                    next_btn.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                _human_wait(0.5, 1.0)
                try:
                    next_btn.click(force=True, timeout=5000)
                except Exception:
                    self._page.evaluate("""
                        () => {
                            const btns = [...document.querySelectorAll('button')];
                            for (const b of btns) {
                                const txt = b.textContent.trim();
                                if ((txt === 'Next Step' || txt === 'ถัดไป') && b.offsetParent !== null) {
                                    b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                _human_wait(1.5, 2.5)
        except Exception as _step1_e:
            logger.debug(f"  [Pre-publish] Step1 復帰判定エラー（無視）: {_step1_e}")

        # ── Pre-publish 診断: Brand の現在値を記録 + 原子的 No Brand + Publish ──
        # Vue.js の nextTick は非同期（マイクロタスクキュー）で走るため、
        # No Brand 選択と Publish クリックを同一 JS evaluate() 内で実行することで
        # Vue が Brand を元に戻す前にフォーム送信できる。
        clicked = False
        try:
            _pre_brand = self._page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    let node;
                    while ((node = walker.nextNode())) {
                        const clean = node.textContent.trim().replace(/[*\\s]/g,'');
                        if (clean !== 'Brand') continue;
                        const par = node.parentElement;
                        if (!par || par.offsetParent === null) continue;
                        let c = par;
                        for (let i = 0; i < 12; i++) {
                            if (!c) break;
                            const sel = c.querySelector('[class*="eds-selector"]');
                            if (sel && sel.offsetParent !== null) {
                                return sel.textContent.trim();
                            }
                            c = c.parentElement;
                        }
                    }
                    return null;
                }
            """)
            logger.info(f"  [Pre-publish] Brand状態: '{_pre_brand}'")
            # Brand が No Brand 以外なら原子的修正（No Brand 選択 + 即時 Publish クリック）
            if _pre_brand and _pre_brand not in ('No Brand', 'ไม่มีแบรนด์'):
                logger.warning(f"  [Pre-publish] Brand='{_pre_brand}' → 原子的 No Brand + Publish を試みる")
                # Step 1: Brand EDS をクリックしてドロップダウンを開く
                _fix_ok = self._page.evaluate("""
                    () => {
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walker.nextNode())) {
                            if (node.textContent.trim().replace(/[*\\s]/g,'') !== 'Brand') continue;
                            const par = node.parentElement;
                            if (!par || par.offsetParent === null) continue;
                            let c = par;
                            for (let i = 0; i < 12; i++) {
                                if (!c) break;
                                const sel = c.querySelector('[class*="eds-selector"]');
                                if (sel && sel.offsetParent !== null) {
                                    sel.scrollIntoView({block:'center'});
                                    sel.click();
                                    return true;
                                }
                                c = c.parentElement;
                            }
                        }
                        return false;
                    }
                """)
                if _fix_ok:
                    _human_wait(1.5, 2.0)  # ドロップダウンアニメーション待ち（長め）
                    # Step 2: 原子的操作 — No Brand 選択 + 即時 Save and Publish クリック
                    # 同一 JS evaluate 内で実行することで Vue の nextTick が走る前に完了する
                    _atomic = self._page.evaluate("""
                        () => {
                            const isVis = (el) => {
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            };
                            // No Brand オプションを探してクリック
                            const noBrandTexts = new Set(['No Brand', 'ไม่มีแบรนด์']);
                            let noBrandClicked = false;
                            // form-item 除外フィルタを外す（ポップアップが form-item 内に描画される場合も対応）
                            const candidates = [...document.querySelectorAll(
                                'li, [role="option"], [class*="option"]'
                            )].filter(el =>
                                isVis(el) &&
                                noBrandTexts.has(el.textContent.trim())
                            );
                            if (candidates.length > 0) {
                                candidates[0].click();
                                noBrandClicked = true;
                            }
                            if (!noBrandClicked) return {noBrand: false, publish: false};
                            // No Brand クリック直後 — Vue の nextTick (非同期) が走る前に
                            // Save and Publish ボタンを同期的にクリックする
                            const btns = [...document.querySelectorAll('button')]
                                .filter(b => b.offsetParent !== null);
                            for (const b of btns) {
                                const txt = b.textContent.trim();
                                if (txt.includes('Save and Publish') ||
                                        txt.includes('Save & Publish') ||
                                        txt.includes('บันทึกและเผยแพร่') ||
                                        txt.includes('เผยแพร่')) {
                                    b.dispatchEvent(new MouseEvent('click', {
                                        bubbles: true, cancelable: true, view: window
                                    }));
                                    return {noBrand: true, publish: true};
                                }
                            }
                            return {noBrand: true, publish: false};
                        }
                    """)
                    logger.info(f"  [Pre-publish] 原子的操作結果: {_atomic}")
                    if _atomic and _atomic.get('publish'):
                        logger.info("  [Pre-publish] No Brand + Save and Publish 原子的実行完了 ✅")
                        clicked = True
                    elif _atomic and _atomic.get('noBrand') and not _atomic.get('publish'):
                        logger.warning("  [Pre-publish] No Brand 選択済みだが Publish ボタン未発見 → 通常フローで続行")
                    else:
                        # 原子的操作失敗 → Playwright ロケーターで再試行（動作実績あり）
                        logger.warning("  [Pre-publish] 原子的操作失敗 → Playwright ロケーターで再試行")
                        try:
                            self._page.keyboard.press("Escape")
                        except Exception:
                            pass
                        _human_wait(0.5, 0.8)
                        # Playwright ロケーターで Brand → No Brand 選択
                        _pw_nb_done = False
                        for _bra_sel2 in [
                            '[class*="option"]:has-text("No Brand")',
                            'li:has-text("No Brand")',
                            '[role="option"]:has-text("No Brand")',
                        ]:
                            try:
                                _bra_opt2 = self._page.locator(_bra_sel2).first
                                if _bra_opt2.count() and _bra_opt2.is_visible(timeout=2000):
                                    _bra_opt2.click()
                                    logger.info(f"  [Pre-publish] Playwright No Brand 選択完了: {_bra_sel2}")
                                    _pw_nb_done = True
                                    _human_wait(0.3, 0.5)
                                    break
                            except Exception:
                                pass
                        if not _pw_nb_done:
                            logger.warning("  [Pre-publish] Playwright No Brand も失敗 → native setter で再試行")
                            # 第3段フォールバック: native setter + input/change dispatch
                            # Vue の watcher が戻す前に v-model の正規経路に値を流す
                            try:
                                _ns_res = self._page.evaluate("""
                                    () => {
                                        const isVisible = (el) => !!(el && el.offsetParent !== null);
                                        const setNativeValue = (el, value) => {
                                            const proto = Object.getPrototypeOf(el);
                                            const desc = Object.getOwnPropertyDescriptor(proto, 'value')
                                                || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                                            if (!desc || !desc.set) return false;
                                            desc.set.call(el, value);
                                            el.dispatchEvent(new InputEvent('input', {
                                                bubbles: true, cancelable: true, composed: true,
                                                data: value, inputType: 'insertText'
                                            }));
                                            el.dispatchEvent(new Event('change', {bubbles: true, cancelable: true}));
                                            el.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
                                            return true;
                                        };
                                        const candidates = [...document.querySelectorAll('input, textarea')]
                                            .filter(isVisible)
                                            .filter(el => {
                                                const t = [
                                                    el.getAttribute('aria-label'),
                                                    el.getAttribute('placeholder'),
                                                    el.getAttribute('name'),
                                                    el.closest('[class*="brand"]')?.textContent || ''
                                                ].join(' ').toLowerCase();
                                                return t.includes('brand') || t.includes('แบรนด์');
                                            });
                                        const input = candidates[0];
                                        if (!input) return {ok: false, reason: 'no brand input'};
                                        return {ok: setNativeValue(input, 'No Brand')};
                                    }
                                """)
                                logger.info(f"  [Pre-publish] native setter 結果: {_ns_res}")
                            except Exception as _ns_e:
                                logger.debug(f"  [Pre-publish] native setter エラー（無視）: {_ns_e}")
                else:
                    logger.warning("  [Pre-publish] Brand EDS が見つからず → そのまま続行")
        except Exception as _pb_e:
            logger.debug(f"  [Pre-publish] Brand診断エラー（無視）: {_pb_e}")

        publish_selectors = [
            'button:has-text("Save and Publish")',
            'button:has-text("Save & Publish")',
            'button:has-text("บันทึกและเผยแพร่")',
            'button:has-text("เผยแพร่")',
            'button:has-text("Publish")',
            '[class*="publish"] button',
        ]
        if not clicked:
            for sel in publish_selectors:
                try:
                    btn = self._page.locator(sel).first
                    if btn.count() and btn.is_visible():
                        logger.info(f"  出品ボタン発見: {sel}")
                        _human_wait(1, 2)
                        # スクロールして表示してからクリック（サイドバーオーバーレイ対策）
                        try:
                            btn.scroll_into_view_if_needed(timeout=3000)
                        except Exception:
                            pass
                        _human_wait(0.5, 1.0)
                        try:
                            btn.click(timeout=5000)
                        except Exception:
                            # force=True でアクショナビリティチェックをバイパス
                            logger.info("  通常クリック失敗 → force=True で再試行")
                            try:
                                btn.click(force=True, timeout=5000)
                            except Exception:
                                # JS直接クリック（最終手段）
                                logger.info("  force click失敗 → JS直接クリック")
                                self._page.evaluate("""
                                    () => {
                                        const btns = [...document.querySelectorAll('button')];
                                        for (const b of btns) {
                                            const txt = b.textContent.trim();
                                            if (txt.includes('Save and Publish') || txt.includes('Save & Publish') || txt.includes('เผยแพร่')) {
                                                b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                                                return true;
                                            }
                                        }
                                        return false;
                                    }
                                """)
                        clicked = True
                        logger.info("  Save and Publish クリック完了 — 結果待ち...")
                        break
                except Exception as e:
                    logger.warning(f"発行ボタン '{sel}' でエラー: {e}")
                    continue

        if not clicked:
            logger.error("❌ 出品ボタンが見つかりません")
            self._screenshot(f"error_{mw_id}_no_publish_btn")
            return None

        # クリック直後のトースト/ダイアログをすぐ撮影（2秒以内に消えることがある）
        _human_wait(1.5, 2.0)
        self._screenshot(f"publish_toast_{mw_id}")

        # 確認ダイアログ（"Are you sure to Save and Publish?"）が出た場合は再クリック
        # JSで直接ボタンを検索・クリック（Playwright locatorのtext合致問題を回避）
        _human_wait(1, 2)
        try:
            clicked_dialog = self._page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    // "Are you sure" ダイアログ内のボタンを探す
                    // ダイアログ内にある場合、背景オーバーレイのz-index上に存在する
                    for (const btn of btns) {
                        if (btn.textContent.trim() === 'Save and Publish' && btn.offsetParent !== null) {
                            const rect = btn.getBoundingClientRect();
                            // ダイアログは画面中央付近（y:200-600, x:400-900）
                            if (rect.top > 150 && rect.top < 600 && rect.left > 300) {
                                btn.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """)
            if clicked_dialog:
                logger.info("  確認ダイアログ検出 → Save and Publish 再クリック(JS)")
                _human_wait(5, 7)
        except Exception as e:
            logger.warning(f"  確認ダイアログ処理エラー: {e}")

        # クリック後 5〜8秒待ってからスクリーンショットを撮る（バリデーションエラー確認）
        _human_wait(5, 8)
        self._screenshot(f"publish_result_{mw_id}")

        # CAPTCHA検出
        if self._detect_captcha():
            logger.error("❌ 出品時にCAPTCHA検出")
            _notify_captcha()
            self._screenshot(f"captcha_{mw_id}")
            return None

        current_url = self._page.url
        logger.info(f"  出品後URL: {current_url}")

        # 成功判定: URLが /product/edit/ や /product/list/ に変わったら成功
        # ※ /portal/product/new のまま → バリデーションエラーで失敗
        # ※ /portal/product/ だけではダメ (/new を除外する)
        if "product/edit" in current_url:
            logger.info("  ✅ 出品成功（product/edit に遷移）")
            self._session_error_count = 0
            return current_url

        if "product/list" in current_url:
            logger.info("  ✅ 出品成功（product/list に遷移）")
            self._session_error_count = 0
            return current_url

        # まだ /portal/product/new にいる場合はエラー
        if "product/new" in current_url:
            logger.error("❌ 出品失敗: URLが product/new のまま（バリデーションエラーの可能性）")
            mw_id_safe = mw_id if mw_id else "unknown"
            # ── タブ巡回前にページ全体の初期エラー状態をキャプチャ（タブクリックでStateが変わる前）──
            try:
                _init_errs = self._page.evaluate("""
                    () => {
                        const r = [];
                        // 赤いエラーテキスト (Shopee は rgb(255,77,79) 系)
                        [...document.querySelectorAll('*')].filter(el => {
                            if (el.offsetParent === null || !el.textContent.trim() ||
                                el.children.length > 3) return false;
                            const cs = window.getComputedStyle(el);
                            return cs.color === 'rgb(255, 77, 79)' || cs.color === 'rgb(235, 77, 75)' ||
                                   cs.color === 'rgb(255, 0, 0)' || cs.color === 'rgb(240, 60, 60)';
                        }).slice(0, 8).forEach(el => r.push('err-text: ' + el.textContent.trim().slice(0, 120)));
                        // form-item error クラス
                        [...document.querySelectorAll('[class*="form-item"][class*="error"], [class*="has-error"]')]
                            .slice(0, 5).forEach(el => {
                                const lbl = el.querySelector('[class*="label"]');
                                const em = el.querySelector('[class*="explain"], [class*="error-msg"], [class*="message"]');
                                r.push(`field-err: ${lbl ? lbl.textContent.trim() : '?'}: ${em ? em.textContent.trim().slice(0,80) : ''}`);
                            });
                        // toast / snackbar（数字のみ・短すぎるテキストはノイズなのでスキップ）
                        [...document.querySelectorAll('[class*="toast"], [class*="snack"], [class*="alert"]')]
                            .filter(el => {
                                const t = el.textContent.trim();
                                return el.offsetParent !== null && t.length >= 10 && !/^\\d+$/.test(t);
                            })
                            .slice(0, 3).forEach(el => r.push('toast: ' + el.textContent.trim().slice(0, 150)));
                        // 全文から "error" を含む visible テキスト
                        const body = document.body.innerText;
                        const m = body.match(/(error|failed|invalid|ไม่ถูกต้อง|ไม่สามารถ|บันทึกไม่สำเร็จ)[^\\n]*/gi);
                        if (m) m.slice(0, 5).forEach(s => r.push('body: ' + s.slice(0, 120)));
                        return r;
                    }
                """)
                if _init_errs:
                    logger.error(f"  [初期エラー状態] {_init_errs}")
                else:
                    logger.info("  [初期エラー状態] エラーテキスト検出なし（クライアントバリデーション未発火の可能性）")
            except Exception:
                pass
            # 各タブを巡回してエラーフィールドを特定 + 必ずスクリーンショットを撮る
            _tab_names = [
                "Basic information", "Specification", "Description",
                "Sales Information", "Shipping", "Others"
            ]
            for _tab in _tab_names:
                try:
                    _tb = self._page.get_by_text(_tab, exact=True).first
                    if not (_tb.count() and _tb.is_visible()):
                        continue
                    _tb.click()
                    _human_wait(0.8, 1.2)
                    # エラー検出 (広めのセレクタ + テキスト検索)
                    _errs = self._page.evaluate("""
                        () => {
                            const results = [];
                            // CSS class ベース
                            document.querySelectorAll(
                                '[class*="form-item"][class*="error"], [class*="has-error"], ' +
                                '[class*="form-item--invalid"], [class*="field-invalid"], ' +
                                '[class*="is-error"], [class*="error-state"]'
                            ).forEach(el => {
                                const lbl = el.querySelector('[class*="label"]');
                                const em  = el.querySelector('[class*="explain"], [class*="error-msg"], [class*="message"], [class*="error"]');
                                const txt = `${lbl ? lbl.textContent.trim().replace('*','') : '?'}: ${em ? em.textContent.trim() : ''}`;
                                if (txt.trim() !== '?:') results.push(txt);
                            });
                            // aria-invalid inputs
                            document.querySelectorAll('input[aria-invalid="true"], select[aria-invalid="true"]').forEach(inp => {
                                const anc = inp.closest('[class*="form-item"]');
                                const lbl = anc ? anc.querySelector('[class*="label"]') : null;
                                results.push(`${lbl ? lbl.textContent.trim() : inp.name || 'input'}: aria-invalid`);
                            });
                            // ページ上のエラーバナーテキスト
                            const banner = document.querySelector('[class*="error-banner"], [class*="alert-error"], .error-message');
                            if (banner) results.push('banner: ' + banner.textContent.trim().slice(0, 100));
                            // 「Failed to save」テキストを含む要素
                            const allText = document.body.innerText;
                            const failMatch = allText.match(/Failed to save[^\\n]*/);
                            if (failMatch) results.push('page-text: ' + failMatch[0]);
                            // Shopee トースト通知（数字のみ・短すぎるは除外）
                            [...document.querySelectorAll(
                                '[class*="toast"], [class*="snack"], [class*="notification"], [class*="alert"]'
                            )].filter(el => {
                                const t = el.textContent.trim();
                                return el.offsetParent !== null && t.length >= 10 && !/^\\d+$/.test(t);
                            })
                              .slice(0, 3)
                              .forEach(el => results.push('toast: ' + el.textContent.trim().slice(0, 150)));
                            // Shopee エラーポップアップ内テキスト
                            const errPop = document.querySelector('[class*="dialog"] [class*="error"], [class*="modal"] [class*="error"]');
                            if (errPop) results.push('popup: ' + errPop.textContent.trim().slice(0, 150));
                            // 赤字エラーメッセージ（color:red / color:#f33 など）
                            [...document.querySelectorAll('*')].filter(el => {
                                if (el.offsetParent === null || !el.textContent.trim()) return false;
                                const cs = window.getComputedStyle(el);
                                return cs.color === 'rgb(255, 0, 0)' || cs.color === 'rgb(240, 60, 60)' ||
                                       cs.color === 'rgb(255, 77, 79)' || cs.color === 'rgb(235, 77, 75)';
                            }).slice(0, 5).forEach(el => results.push('red-text: ' + el.textContent.trim().slice(0, 100)));
                            return results;
                        }
                    """)
                    if _errs:
                        logger.error(f"  [{_tab}] エラー検出: {_errs}")
                    # 必ずスクリーンショット（デバッグ用）
                    self._screenshot(f"debug_{mw_id_safe}_fail_{_tab.replace(' ', '_')}")
                except Exception:
                    pass
            return None

        # その他のURLの場合（リダイレクト等）
        if "login" not in current_url:
            logger.info(f"  ✅ 出品成功（URL変更: {current_url[:60]}）")
            return current_url

        logger.error(f"❌ 出品失敗: ログインページにリダイレクト")
        return None

    # ─── スクリーンショット ────────────────────────────────

    def _screenshot(self, name: str):
        """エラー時のスクリーンショット保存"""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = ERRORS_DIR / f"{name}_{ts}.png"
            self._page.screenshot(path=str(path))
            logger.info(f"スクリーンショット保存: {path}")
        except Exception as e:
            logger.warning(f"スクリーンショット失敗: {e}")

    # ─── コンテキストマネージャ ────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


# ─── テスト実行 ────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    browser = ShopeeBrowser()
    browser.start()
    try:
        ok = browser.login()
        if ok:
            print("✅ ログイン成功！Cookie保存済み。")
            print("   セラーセンターに移動中...")
            browser._page.goto("https://seller.shopee.co.th/portal/product/list/all", timeout=30000)
            browser._page.wait_for_load_state("networkidle", timeout=15000)
            print("   ブラウザを30秒後に閉じます（確認してください）")
            _human_wait(30, 31)
        else:
            print("❌ ログイン失敗。エラー画像を確認:")
            print(f"   open {ERRORS_DIR}")
            _human_wait(10, 11)
    finally:
        browser.stop()
