"""
Shopee セラーセンター Playwright ブラウザ自動化
seller.shopee.co.th を実際のChrome操作で出品する
"""
import json
import logging
import os
import random
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout

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

    # ─── 起動・終了 ───────────────────────────────────────

    def start(self):
        """ブラウザを起動してコンテキストを初期化"""
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=BROWSER_SETTINGS["headless"],
            slow_mo=BROWSER_SETTINGS["slow_mo"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
        )

        # Cookieが保存済みならロード
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
        # Playwright検出回避
        self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        logger.info("✅ ブラウザ起動完了")

    def stop(self):
        """ブラウザを終了"""
        try:
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
        if COOKIES_FILE.exists():
            logger.info("Cookie確認中...")
            if self._is_logged_in():
                logger.info("✅ Cookie有効 — ログインスキップ")
                return True
            logger.info("Cookie期限切れ — 再ログインします")

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

            file_input.set_input_files(valid_paths[:9])  # Shopee最大9枚
            _human_wait(2, 4)  # アップロード完了待ち

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

            # まずダッシュボードに移動
            self._page.goto(SHOPEE_SELLER_URL, timeout=30000)
            self._page.wait_for_load_state("domcontentloaded", timeout=15000)
            _human_wait(2, 3)

            # 出品ページへ直接移動
            self._page.goto(
                f"{SHOPEE_SELLER_URL}/portal/product/new",
                timeout=30000,
            )
            self._page.wait_for_load_state("domcontentloaded", timeout=15000)
            logger.info(f"  URL: {self._page.url}")
            _human_wait(2, 3)

            # 出品ページ上のモーダル（プロモーション等）を閉じる
            self._dismiss_listing_modals()
            _human_wait(1, 2)

            if self._detect_captcha():
                logger.error("❌ 出品ページでCAPTCHA検出")
                _notify_captcha()
                return None

            # セッション切れ確認
            if "login" in self._page.url:
                logger.info("セッション切れ — 再ログイン")
                if not self.login():
                    return None
                self._page.goto(f"{SHOPEE_SELLER_URL}/portal/product/add", timeout=30000)
                _human_wait(2, 4)

            # ════════════════════════════════════════
            # STEP 1: 商品名 + 画像
            # ════════════════════════════════════════
            title = str(product.get("title_th") or product.get("title_en") or "3D Print Item")[:120]

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
                self._page.wait_for_selector(title_sel, timeout=15000)
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

            # 「Next Step」ボタンをクリック
            next_sel = 'button:has-text("Next Step"), button:has-text("ถัดไป")'
            try:
                self._page.wait_for_selector(next_sel, timeout=8000)
                _human_wait(0.5, 1.0)
                self._page.locator(next_sel).first.click()
                self._page.wait_for_load_state("domcontentloaded", timeout=15000)
                logger.info("  Next Step クリック完了")
                _human_wait(2, 3)
            except Exception as e:
                logger.warning(f"Next Step ボタンが見つかりません（続行）: {e}")
                self._screenshot(f"error_{mw_id}_nextstep")

            # ════════════════════════════════════════
            # STEP 2: カテゴリ・説明・価格・在庫
            # ════════════════════════════════════════

            # カテゴリ選択
            self._select_category(product.get("category", ""))
            _human_wait(1, 1.5)

            # 商品説明入力
            description = str(
                product.get("description_th") or "สินค้า 3D Printed คุณภาพสูง"
            )[:3000]
            try:
                desc_sel = (
                    '.ql-editor, '
                    'div[contenteditable="true"], '
                    'textarea[placeholder*="Description"], '
                    'textarea[placeholder*="รายละเอียด"]'
                )
                self._page.wait_for_selector(desc_sel, timeout=10000)
                desc_el = self._page.locator(desc_sel).first
                desc_el.click()
                _human_wait(0.3, 0.6)
                desc_el.fill(description)
                logger.info("  説明入力完了")
            except Exception as e:
                logger.warning(f"説明入力エラー（続行）: {e}")

            _human_wait(1, 1.5)
            _random_scroll(self._page)

            # 価格入力（入力欄をlabelで探す）
            price = float(product.get("price_thb") or LISTING_SETTINGS["min_price_thb"])
            try:
                price_sel = (
                    'input[placeholder*="Price"], '
                    'input[placeholder*="ราคา"], '
                    'label:has-text("Price") ~ div input, '
                    'label:has-text("Price") + div input'
                )
                self._page.wait_for_selector(price_sel, timeout=10000)
                price_input = self._page.locator(price_sel).first
                price_input.triple_click()
                _human_wait(0.2, 0.4)
                price_input.fill(str(int(price)))
                logger.info(f"  価格入力完了: {int(price)} THB")
            except Exception as e:
                logger.warning(f"価格入力エラー（続行）: {e}")

            _human_wait(0.8, 1.2)

            # 在庫数入力
            stock = LISTING_SETTINGS["default_stock"]
            try:
                stock_sel = (
                    'input[placeholder*="Stock"], '
                    'input[placeholder*="สต็อก"], '
                    'label:has-text("Stock") ~ div input, '
                    'label:has-text("Stock") + div input'
                )
                stock_el = self._page.locator(stock_sel).first
                if stock_el.count():
                    stock_el.triple_click()
                    _human_wait(0.2, 0.4)
                    stock_el.fill(str(stock))
                    logger.info(f"  在庫入力完了: {stock}")
            except Exception as e:
                logger.warning(f"在庫入力エラー（続行）: {e}")

            _human_wait(0.8, 1.2)
            _random_scroll(self._page)

            # ── Save & Publish ────────────────────────────
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

    def _dismiss_listing_modals(self):
        """出品フォームに表示されるプロモーション・案内モーダルを閉じる"""
        close_patterns = [
            # 「Got it」「OK」「閉じる」系
            'button:has-text("Got it")',
            'button:has-text("Got It")',
            'button:has-text("OK")',
            'button:has-text("Close")',
            'button:has-text("Skip")',
            'button:has-text("Maybe Later")',
            'button:has-text("No, thanks")',
            # ×ボタン
            '[class*="modal"] [class*="close"]',
            '[class*="dialog"] [class*="close"]',
            '[aria-label="Close"]',
            'svg[class*="close"]',
            # Shopee固有
            '[class*="modal-close"]',
            '[class*="popup-close"]',
        ]
        for sel in close_patterns:
            try:
                btn = self._page.locator(sel).first
                if btn.count() and btn.is_visible():
                    btn.click()
                    logger.info(f"  モーダルを閉じました: {sel}")
                    _human_wait(0.5, 1.0)
            except Exception:
                pass
        # ESCでも試みる
        try:
            self._page.keyboard.press("Escape")
            _human_wait(0.5, 1.0)
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
        publish_selectors = [
            'button:has-text("Save & Publish")',
            'button:has-text("Publish")',
            'button:has-text("บันทึกและเผยแพร่")',
            'button:has-text("เผยแพร่")',
            '[class*="publish"] button',
            '[class*="submit"] button[type="submit"]',
        ]
        for sel in publish_selectors:
            try:
                btn = self._page.locator(sel).first
                if btn.count():
                    _human_wait(1, 2)
                    btn.click()
                    _human_wait(3, 6)

                    # CAPTCHA検出
                    if self._detect_captcha():
                        logger.error("❌ 出品時にCAPTCHA検出")
                        _notify_captcha()
                        self._screenshot(f"captcha_{mw_id}")
                        return None

                    # 成功確認（URLが商品詳細ページに変わるか、成功メッセージ）
                    current_url = self._page.url
                    success_indicators = [
                        "product/edit",
                        "product/list",
                        "/portal/product/",
                    ]
                    for indicator in success_indicators:
                        if indicator in current_url:
                            return current_url

                    # 成功メッセージのテキスト確認
                    try:
                        self._page.wait_for_selector(
                            ':has-text("สำเร็จ"), :has-text("success"), :has-text("published")',
                            timeout=5000,
                        )
                        return self._page.url
                    except PlaywrightTimeout:
                        pass

                    # URLが変わったら成功とみなす
                    if "add" not in current_url:
                        return current_url

            except Exception as e:
                logger.warning(f"発行ボタン '{sel}' でエラー: {e}")
                continue

        logger.error("❌ 出品ボタンが見つかりません")
        self._screenshot(f"error_{mw_id}_publish")
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
