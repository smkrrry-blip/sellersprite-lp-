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

            # 出品ページへ直接移動
            self._page.goto(
                f"{SHOPEE_SELLER_URL}/portal/product/new",
                timeout=60000,
                wait_until="domcontentloaded",
            )
            _human_wait(2, 3)

            # ページをリロードしてドラフト復元をクリア
            # (デバッグで確認: reload後は常にクリーンなフォームが読み込まれる)
            self._page.reload(wait_until="domcontentloaded", timeout=60000)
            # React アプリのレンダリングを待つ（networkidle で JS実行完了を確認）
            try:
                self._page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass  # networkidle がタイムアウトしても続行
            _human_wait(3, 5)

            logger.info(f"  URL: {self._page.url}")

            # sspSearchTour のみ削除（他のオーバーレイは削除しない）
            self._page.evaluate("document.getElementById('sspSearchTour')?.remove()")
            _human_wait(0.5, 1.0)

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
                _human_wait(3, 4)
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

                    # Brand フィールドは EDS Select ドロップダウン（"Please select" が placeholder）
                    # ※ Playwright CSS locator は EDS コンポーネントを見つけられない（Shadow DOM 疑い）
                    # ※ input[placeholder*="Brand"] は商品名フィールドに一致するため使用禁止
                    # → JS evaluate で querySelectorAll('[class*="eds-selector"]') を使う（診断で確認済み）
                    brand_filled = False
                    try:
                        # まずページを少し下にスクロール（Specification セクションを表示）
                        self._page.mouse.wheel(0, 400)
                        _human_wait(0.5, 0.8)

                        # JS evaluate でクリック
                        # ── "* Brand" ラベルを起点に EDS selector を探す ──
                        # （以前の single-selector フィルタは全 46 件をミスするため廃止）
                        brand_click = self._page.evaluate("""
                            () => {
                                // ① ラベルテキストで親コンテナを特定 → その中の eds-selector をクリック
                                const labels = [...document.querySelectorAll(
                                    'label, .eds-form-item__label, [class*="form-item__label"]'
                                )];
                                const brandLabel = labels.find(
                                    l => l.textContent.trim().replace('*','').trim() === 'Brand'
                                );
                                if (brandLabel) {
                                    let container = brandLabel.parentElement;
                                    for (let i = 0; i < 6; i++) {
                                        if (!container) break;
                                        const sel = container.querySelector('[class*="eds-selector"]');
                                        if (sel) {
                                            sel.scrollIntoView({block: 'center'});
                                            sel.click();
                                            return {found: true, method: 'label_parent',
                                                    cls: sel.className.substring(0, 60)};
                                        }
                                        container = container.parentElement;
                                    }
                                }
                                // ② EDS selector の祖先に "Brand" テキストを持つものを探す
                                const allEds = [...document.querySelectorAll('[class*="eds-selector"]')];
                                for (const el of allEds) {
                                    let p = el.parentElement;
                                    for (let i = 0; i < 4; i++) {
                                        if (!p) break;
                                        const t = (p.textContent || '');
                                        if (t.includes('Brand') && !t.includes('Variation')
                                                && !t.includes('Shipping')) {
                                            el.scrollIntoView({block: 'center'});
                                            el.click();
                                            return {found: true, method: 'ancestor_text',
                                                    cls: el.className.substring(0, 60)};
                                        }
                                        p = p.parentElement;
                                    }
                                }
                                // ③ フォールバック: single-selector（clearable なし）
                                const candidates = allEds.filter(el => {
                                    const cls = el.className || '';
                                    return cls.includes('single-selector') && !cls.includes('clearable');
                                });
                                if (candidates.length > 0) {
                                    candidates[0].scrollIntoView({block: 'center'});
                                    candidates[0].click();
                                    return {found: true, method: 'single_selector',
                                            cls: candidates[0].className.substring(0, 60)};
                                }
                                return {found: false, totalEds: allEds.length,
                                        labelFound: !!brandLabel};
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
                else:
                    logger.warning("  ⚠️ Basic Info タブが見つかりません")
            except Exception as e:
                logger.warning(f"  Basic Infoタブエラー（続行）: {e}")

            _human_wait(0.8, 1.2)

            # ── Description タブ ──────────────────────
            try:
                desc_tab = self._page.get_by_text("Description", exact=True).first
                if desc_tab.count():
                    desc_tab.click()
                    _human_wait(1.5, 2.5)
                    desc_el = self._page.locator('div[contenteditable="true"], .ql-editor').first
                    if desc_el.count() and desc_el.is_visible():
                        desc_el.click()
                        _human_wait(0.3, 0.5)
                        desc_el.fill(description)
                        logger.info("  説明入力完了")
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

                    # ── カテゴリ必須バリエーション対応 ──────────────────────────
                    # カテゴリ "Docks & Stands" 等はサーバー側でバリエーション必須。
                    # "Enable Variations" ボタンが表示されていればクリックして _fill_variation_pricing() で設定。
                    enable_var_btn = self._page.locator('button:has-text("Enable Variations")').first

                    if enable_var_btn.count() and enable_var_btn.is_visible():
                        logger.info("  'Enable Variations' ボタン検出 → バリエーション設定開始")
                        enable_var_btn.click()
                        _human_wait(2, 3)
                        self._screenshot(f"debug_{mw_id}_variation_enabled")
                        price_filled, stock_filled = self._fill_variation_pricing(price, stock)
                        logger.info(f"  バリエーション設定完了: price={price_filled}, stock={stock_filled}")
                    else:
                        # バリエーション不要 → 通常の価格・在庫入力
                        logger.info("  バリエーションなし → 通常価格入力")

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
                                price_inp.click()
                                _human_wait(0.2, 0.3)
                                price_inp.fill(str(int(price)))
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

                        # 在庫入力 (placeholder="-")
                        _human_wait(0.5, 1.0)
                        try:
                            stock_inp = self._page.locator('input[placeholder="-"]').first
                            stock_inp.wait_for(state="visible", timeout=5000)
                            stock_inp.click()
                            _human_wait(0.1, 0.2)
                            stock_inp.fill(str(stock))
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
                            logger.info(f"  在庫入力完了: {stock}")
                            stock_filled = True
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
                shipping_tab = self._page.get_by_text("Shipping", exact=True).first
                if shipping_tab.count():
                    shipping_tab.click()
                    _human_wait(2, 3)

                    # 重量入力: .price-input コンテナの中で parent text="kg" のもの
                    weight_filled = False
                    try:
                        weight_inp = self._page.locator('.price-input').filter(
                            has_text="kg"
                        ).locator('input.eds-input__input').first
                        weight_inp.wait_for(state="visible", timeout=5000)
                        weight_inp.scroll_into_view_if_needed()
                        weight_inp.click()
                        _human_wait(0.2, 0.3)
                        weight_inp.fill(f"{weight_kg:.2f}")
                        self._page.keyboard.press("Tab")
                        logger.info(f"  重量入力完了: {weight_kg:.2f} kg")
                        weight_filled = True
                    except Exception as e:
                        logger.warning(f"  重量入力エラー: {e}")

                    # 配送オプションを有効化
                    _human_wait(1, 1.5)
                    enable_shipping_done = False

                    # Enable ボタンをクリックしてダイアログを開く
                    enable_btn = self._page.locator('button:has-text("Enable")').first
                    if enable_btn.count() and enable_btn.is_visible():
                        enable_btn.click()
                        logger.info("  配送: Enable クリック → ダイアログ待ち")
                        _human_wait(3, 4)  # ダイアログが完全に開くのを待つ

                    # Apply & Enable Channel を JavaScript で直接クリック
                    # (Playwrightのvisibility checkをバイパス)
                    try:
                        clicked = self._page.evaluate("""
                            () => {
                                const btns = [...document.querySelectorAll('button')];
                                for (const btn of btns) {
                                    if (btn.textContent.trim().includes('Apply & Enable Channel')) {
                                        btn.click();
                                        return true;
                                    }
                                }
                                return false;
                            }
                        """)
                        if clicked:
                            logger.info("  配送: Apply & Enable Channel クリック(JS)")
                            _human_wait(2, 3)
                            enable_shipping_done = True
                        else:
                            logger.warning("  Apply & Enable Channel ボタンが見つかりません")
                    except Exception as e:
                        logger.warning(f"  Apply & Enable Channel JSエラー: {e}")

                    if not enable_shipping_done:
                        logger.warning("  ⚠️ 配送オプションを有効化できませんでした")

            except Exception as e:
                logger.warning(f"Shippingタブエラー（続行）: {e}")

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

    def _deactivate_variations(self):
        """
        Sales Information タブで Variations が誤って有効化されている場合に無効化する。
        Variation1 入力欄が空でエラー状態の場合、×ボタンまたはJSで行を削除する。
        """
        try:
            # Variation削除ボタン (×) を探してクリック
            # Shopeeのバリエーション行には削除ボタンがある
            del_selectors = [
                '[class*="variation"] [class*="delete"]',
                '[class*="variation"] [class*="remove"]',
                '[class*="variation-item"] button',
                '[class*="var-item"] [class*="close"]',
                '[class*="variation-name"] + button',
            ]
            deleted = False
            for sel in del_selectors:
                try:
                    btns = self._page.locator(sel).all()
                    if btns:
                        for btn in btns:
                            if btn.is_visible():
                                btn.click(force=True)
                                _human_wait(0.5, 1.0)
                                deleted = True
                                logger.info(f"  Variation削除: {sel}")
                except Exception:
                    pass

            if not deleted:
                # JavaScriptでVariation行のinputをクリア
                # Variationが「active」かどうかを確認
                # Variation1 の input value が空でなければ、それをクリアする
                cleared = self._page.evaluate("""
                    () => {
                        // Variation1 入力欄を見つける
                        // placeholder が "Variation 1" or class が variation-name 的なものを探す
                        const possibleVarInputs = [...document.querySelectorAll('input')]
                            .filter(inp => {
                                if (inp.offsetParent === null) return false;
                                const ph = inp.placeholder || '';
                                const parentText = inp.parentElement?.textContent || '';
                                return ph.toLowerCase().includes('variation') ||
                                       parentText.includes('Variation 1') ||
                                       parentText.includes('Variation1');
                            });
                        if (possibleVarInputs.length > 0) {
                            possibleVarInputs.forEach(inp => {
                                const setter = Object.getOwnPropertyDescriptor(
                                    HTMLInputElement.prototype, 'value'
                                ).set;
                                setter.call(inp, '');
                                inp.dispatchEvent(new Event('input', {bubbles: true}));
                                inp.dispatchEvent(new Event('change', {bubbles: true}));
                            });
                            return true;
                        }
                        return false;
                    }
                """)
                if cleared:
                    logger.info("  Variation入力をJSでクリア")
        except Exception as e:
            logger.warning(f"  Variation無効化エラー（続行）: {e}")

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
                        const nameInp = inputs.find(i => i.placeholder.includes('Color'));
                        const optInp  = inputs.find(i => i.placeholder.includes('Red'));
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

            # ── Step 3: 価格入力（placeholder="Price"、grandparent='฿'） ──
            try:
                price_inp = self._page.locator('input[placeholder="Price"]').first
                if price_inp.count() and price_inp.is_visible():
                    price_inp.scroll_into_view_if_needed()
                    price_inp.click()
                    _human_wait(0.2, 0.3)
                    price_inp.fill(str(int(price)))
                    self._page.keyboard.press("Tab")
                    logger.info(f"  Variation価格入力: {int(price)} THB")
                    price_filled = True
                else:
                    logger.warning("  placeholder='Price' が見つからない — L1_baht fallback")
                    price_marked = self._page.evaluate("""
                        () => {
                            document.querySelectorAll('[data-bot-price]').forEach(
                                e => e.removeAttribute('data-bot-price'));
                            const inputs = [...document.querySelectorAll('input.eds-input__input')]
                                .filter(i => i.offsetParent !== null && !i.readOnly);
                            for (const inp of inputs) {
                                const gp = inp.parentElement?.parentElement;
                                if (gp && gp.textContent.trim() === '\u0e3f') {
                                    inp.setAttribute('data-bot-price', 'true');
                                    inp.scrollIntoView({block: 'center'});
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                    if price_marked:
                        pi = self._page.locator('[data-bot-price="true"]').first
                        pi.click()
                        _human_wait(0.2, 0.3)
                        pi.fill(str(int(price)))
                        self._page.keyboard.press("Tab")
                        logger.info(f"  Variation価格(L1_baht): {int(price)} THB")
                        price_filled = True
            except Exception as e:
                logger.warning(f"  Variation価格入力エラー: {e}")

            # ── Step 4: 在庫入力（placeholder="Stock"） ────────────
            _human_wait(0.3, 0.5)
            try:
                stock_inp = self._page.locator('input[placeholder="Stock"]').first
                if not (stock_inp.count() and stock_inp.is_visible()):
                    stock_inp = self._page.locator('input[placeholder="-"]').first
                stock_inp.wait_for(state="visible", timeout=5000)
                stock_inp.scroll_into_view_if_needed()
                stock_inp.click()
                _human_wait(0.1, 0.2)
                stock_inp.fill(str(stock))
                self._page.keyboard.press("Tab")
                _human_wait(0.3, 0.5)
                logger.info(f"  Variation在庫入力: {stock}")
                stock_filled = True
            except Exception as e:
                logger.warning(f"  Variation在庫入力エラー: {e}")

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
        publish_selectors = [
            'button:has-text("Save and Publish")',
            'button:has-text("Save & Publish")',
            'button:has-text("บันทึกและเผยแพร่")',
            'button:has-text("เผยแพร่")',
            'button:has-text("Publish")',
            '[class*="publish"] button',
        ]
        clicked = False
        for sel in publish_selectors:
            try:
                btn = self._page.locator(sel).first
                if btn.count() and btn.is_visible():
                    logger.info(f"  出品ボタン発見: {sel}")
                    _human_wait(1, 2)
                    btn.click()
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
            return current_url

        if "product/list" in current_url:
            logger.info("  ✅ 出品成功（product/list に遷移）")
            return current_url

        # まだ /portal/product/new にいる場合はエラー
        if "product/new" in current_url:
            logger.error("❌ 出品失敗: URLが product/new のまま（バリデーションエラーの可能性）")
            # ページテキストからエラーメッセージを取得
            try:
                error_text = self._page.locator(
                    '[class*="error"], [class*="warning"], [class*="alert"], '
                    '.error-msg, .form-error, [style*="color: red"]'
                ).all_text_contents()
                if error_text:
                    logger.error(f"  バリデーションエラー: {error_text[:5]}")
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
