"""
Shopee セラーセンター Playwright ブラウザ自動化
seller.shopee.co.th を実際のChrome操作で出品する
"""
import json
import logging
import os
import random
import re
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


def _inject_no_brand_recursive(obj: dict, depth: int = 0) -> None:
    """Fix17: API レスポンスに No Brand エントリを注入し、全ブランドの is_mandatory を 0 に設定。
    モジュールレベル関数としてクラス名参照を回避。
    """
    if not obj or depth > 8 or not isinstance(obj, dict):
        return
    brand_keys = ('brand_list', 'brands', 'brandList', 'brand_options',
                  'data', 'result', 'response')
    for key in brand_keys:
        val = obj.get(key)
        if not isinstance(val, list) or not val:
            if isinstance(val, dict):
                _inject_no_brand_recursive(val, depth + 1)
            continue
        first = val[0] if val else {}
        if not isinstance(first, dict):
            continue
        is_brand_arr = ('brand_id' in first or 'brand_name' in first or 'brandId' in first)
        if is_brand_arr:
            # すべてのブランドの is_mandatory / isMandatory を 0 に設定（ACONATIC 強制解除）
            for b in val:
                if isinstance(b, dict):
                    for mf in ('is_mandatory', 'isMandatory', 'mandatory'):
                        if mf in b:
                            b[mf] = 0
            has_nb = any(
                b.get('brand_id') == 0 or b.get('brandId') == 0 or
                'no brand' in str(b.get('brand_name', b.get('name', ''))).lower()
                for b in val if isinstance(b, dict)
            )
            if not has_nb:
                nb = {k: (0 if isinstance(v, (int, float)) else '') for k, v in first.items()}
                nb.update({
                    'brand_id': 0, 'brandId': 0,
                    'brand_name': 'No Brand', 'name': 'No Brand',
                    'display_name': 'No Brand', 'brandName': 'No Brand',
                    'require_license': 0, 'requireLicense': 0,
                    'is_mandatory': 0, 'isMandatory': 0,
                    'status': 1,
                })
                val.insert(0, nb)
                logger.info(f"[Fix17] No Brand 注入 + is_mandatory=0: key={key}, total={len(val)}")
        else:
            for item in val:
                if isinstance(item, dict):
                    _inject_no_brand_recursive(item, depth + 1)
    # Recurse into non-brand-key nested dicts
    for key, val in obj.items():
        if key not in brand_keys and isinstance(val, dict):
            _inject_no_brand_recursive(val, depth + 1)


def _inject_brand_attribute_optional(obj: dict, depth: int = 0) -> None:
    """Fix17: get_recommend_attribute / get_content_filling_suggestion レスポンスを改変。
    - attributes / attribute_list の Brand属性を optional (is_mandatory=0) に変更
    - brand, brand_id, default_brand 等のトップレベルフィールドをクリア
    - content_fill_suggestion の brand フィールドをクリア
    """
    if not obj or depth > 8 or not isinstance(obj, dict):
        return

    # トップレベルの brand/mandatory 系フィールドをゼロ化
    for key in list(obj.keys()):
        kl = key.lower()
        if any(x in kl for x in ('brand_id', 'brand_name', 'default_brand',
                                   'mandatory_brand', 'recommend_brand')):
            v = obj[key]
            if isinstance(v, int):
                obj[key] = 0
            elif isinstance(v, str) and v:
                obj[key] = ''
            elif isinstance(v, dict):
                obj[key] = {}
        if 'is_mandatory' in kl or 'ismandatory' in kl:
            if isinstance(obj[key], (int, float, bool)):
                obj[key] = 0

    # attributes 配列を検索してBrand属性を optional に
    for attr_key in ('attributes', 'attribute_list', 'attributeList', 'attrs',
                     'data', 'result', 'response'):
        val = obj.get(attr_key)
        if isinstance(val, list):
            for item in val:
                if not isinstance(item, dict):
                    continue
                name = str(item.get('name', item.get('attribute_name',
                           item.get('attributeName', '')))).lower()
                if 'brand' in name:
                    # Brand属性を optional に
                    for mf in ('is_mandatory', 'isMandatory', 'mandatory', 'required'):
                        if mf in item:
                            item[mf] = 0
                    # デフォルト値をクリア
                    for df in ('default_value', 'defaultValue', 'value',
                               'recommended_value', 'recommendedValue',
                               'recommend_value_id', 'value_id', 'valueId'):
                        if df in item:
                            v = item[df]
                            item[df] = 0 if isinstance(v, (int, float)) else ([] if isinstance(v, list) else '')
                    logger.info(f"[Fix17] Brand属性 optional化: name={name}")
                else:
                    # 再帰
                    _inject_brand_attribute_optional(item, depth + 1)
        elif isinstance(val, dict):
            _inject_brand_attribute_optional(val, depth + 1)


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
                # Fix8: 既存ページを再利用（new_page()はSSLキャッシュなしで失敗する）
                existing_pages = self._context.pages
                shopee_pages = [p for p in existing_pages if "shopee" in p.url]
                if shopee_pages:
                    self._page = shopee_pages[0]
                    logger.info(f"✅ CDP接続成功 (port {CDP_PORT}) — 既存Shopeeタブ再利用: {self._page.url[:60]}")
                elif existing_pages:
                    self._page = existing_pages[0]
                    logger.info(f"✅ CDP接続成功 (port {CDP_PORT}) — 既存タブ再利用: {self._page.url[:60]}")
                else:
                    self._page = self._context.new_page()
                    logger.info(f"✅ CDP接続成功 (port {CDP_PORT}) — 新規タブ作成")
                self._using_cdp = True
                # Fix17: SW登録ブロック + page.route() 設置（Fix15より確実な手法）
                try:
                    self._context.add_init_script(self._FIX17_SW_BLOCK_JS)
                    logger.info("[Fix17] SW.register() ブロック init_script を context に設置")
                except Exception as _is_e:
                    logger.warning(f"[Fix17] init_script設置失敗: {_is_e}")
                # 既存 SW を今すぐ unregister（現在のタブに効果）
                self._unregister_service_workers()
                # page.route() でブランドAPIをインターセプト
                self._setup_fix17_route()
                # Fix15 JS interceptor も belt-and-suspenders で維持
                self._setup_brand_api_intercept()
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
        # Fix17: SW登録ブロック
        try:
            self._context.add_init_script(self._FIX17_SW_BLOCK_JS)
            logger.info("[Fix17] SW.register() ブロック init_script を context に設置")
        except Exception as _is_e:
            logger.warning(f"[Fix17] init_script設置失敗: {_is_e}")
        self._setup_fix17_route()
        self._setup_brand_api_intercept()
        logger.info("✅ ブラウザ起動完了")

    # ── Fix15 brand JS interceptor ─────────────────────────────────────────
    _FIX15_JS = r"""
(function() {
    if (window.__fix15Installed) return 'already';
    window.__fix15Installed = true;

    const KEYWORDS = ['brand', 'recommend', 'category_attribute', 'fill_suggest',
                      'attribute', 'get_brand', 'brand_list',
                      'check_mpsku',   // Fix19: MPSKU catalog matching → clears forced brand
                      'mpsku_for_edit' // Fix19: same
                      ];

    function isBrandUrl(url) {
        const u = String(url).toLowerCase();
        return KEYWORDS.some(k => u.includes(k));
    }

    // Fix19: スカラー mandatory_brand_id / default_brand_id をゼロ化
    function zeroMandatoryBrandScalars(obj, depth) {
        if (!obj || depth > 8 || typeof obj !== 'object' || Array.isArray(obj)) return;
        for (const k of Object.keys(obj)) {
            const kl = k.toLowerCase();
            if (['mandatory_brand_id','mandatory_brand','default_brand_id','default_brand',
                 'required_brand_id','required_brand','mpsku_brand_id','brand_id_mandatory',
                 'mpsku_info','has_mpsku'].some(p => kl.includes(p.replace('_','')))) {
                const v = obj[k];
                if (typeof v === 'number') obj[k] = 0;
                else if (typeof v === 'string' && v) obj[k] = '';
                else if (typeof v === 'boolean') obj[k] = false;
                else if (v && typeof v === 'object') {
                    // For mpsku_info sub-object, zero out brand fields
                    for (const bk of Object.keys(v)) {
                        if (bk.toLowerCase().includes('brand')) {
                            if (typeof v[bk] === 'number') v[bk] = 0;
                            else if (typeof v[bk] === 'string') v[bk] = '';
                        }
                    }
                    if ('has_mpsku' in v) v.has_mpsku = false;
                }
                console.warn('[Fix19] Zeroed mandatory brand scalar:', k, '->', obj[k]);
            }
            if (obj[k] && typeof obj[k] === 'object' && !Array.isArray(obj[k])) {
                zeroMandatoryBrandScalars(obj[k], depth + 1);
            }
        }
    }

    function injectNoBrand(obj, depth) {
        if (!obj || depth > 8) return obj;
        if (typeof obj !== 'object') return obj;
        if (Array.isArray(obj)) {
            for (let i = 0; i < obj.length; i++) obj[i] = injectNoBrand(obj[i], depth + 1);
            return obj;
        }
        zeroMandatoryBrandScalars(obj, 0);  // Fix19: zero mandatory scalars first
        for (const key of ['brand_list', 'brands', 'brandList', 'brand_options',
                           'data', 'result', 'response']) {
            if (!obj[key]) continue;
            if (Array.isArray(obj[key])) {
                const arr = obj[key];
                // brand-like array: items with brand_id or brand_name
                const isBrandArr = arr.length > 0 && arr[0] &&
                    ('brand_id' in arr[0] || 'brand_name' in arr[0] || 'brandId' in arr[0]);
                if (isBrandArr) {
                    // すべてのブランドの is_mandatory を 0 に（ACONATIC 強制解除）
                    for (const b of arr) {
                        if (b && typeof b === 'object') {
                            if ('is_mandatory' in b) b.is_mandatory = 0;
                            if ('isMandatory' in b) b.isMandatory = 0;
                            if ('mandatory' in b) b.mandatory = 0;
                        }
                    }
                    const hasNB = arr.some(b =>
                        b.brand_id === 0 || b.brandId === 0 ||
                        String(b.brand_name || b.name || '').toLowerCase().includes('no brand')
                    );
                    if (!hasNB) {
                        const s = arr[0] || {};
                        const nb = {};
                        for (const k in s) nb[k] = (typeof s[k] === 'number' ? 0 : '');
                        Object.assign(nb, {
                            brand_id: 0, brandId: 0,
                            brand_name: 'No Brand', name: 'No Brand',
                            display_name: 'No Brand', brandName: 'No Brand',
                            require_license: 0, requireLicense: 0,
                            is_mandatory: 0, isMandatory: 0,
                            status: 1,
                        });
                        arr.unshift(nb);
                        console.warn('[Fix15] No Brand 注入 + is_mandatory=0:', key, JSON.stringify(nb));
                    }
                } else {
                    for (let i = 0; i < arr.length; i++) arr[i] = injectNoBrand(arr[i], depth + 1);
                }
            } else {
                obj[key] = injectNoBrand(obj[key], depth + 1);
            }
        }
        return obj;
    }

    // ── Fix19: POST リクエスト brand_id 強制ゼロ化 ────────────────────────
    function patchPostBody(bodyStr) {
        try {
            const obj = JSON.parse(bodyStr);
            let patched = false;
            const patchObj = (o) => {
                if (!o || typeof o !== 'object') return;
                for (const k of ['brand_id', 'brandId']) {
                    if (k in o && typeof o[k] === 'number' && o[k] !== 0) {
                        console.warn('[Fix19] POST ' + k + '=' + o[k] + ' → 0');
                        o[k] = 0; patched = true;
                    }
                }
                for (const k of ['brand_license_id', 'brandLicenseId', 'license_id']) {
                    if (k in o) { delete o[k]; patched = true; }
                }
                // ネスト1段
                for (const nested of ['item_info', 'product_info', 'data', 'item', 'product']) {
                    if (o[nested] && typeof o[nested] === 'object') patchObj(o[nested]);
                }
            };
            patchObj(obj);
            return patched ? JSON.stringify(obj) : null;
        } catch(e) { return null; }
    }

    // ── fetch override ────────────────────────────────────────────────────
    const _origFetch = window.fetch;
    window.fetch = async function(input, init) {
        const url = (input instanceof Request) ? input.url : String(input);
        const method = (init && init.method) ? String(init.method).toUpperCase() : 'GET';

        // Fix19: POST ボディの brand_id を 0 に強制
        if (method === 'POST' && init && init.body && typeof init.body === 'string') {
            const patched = patchPostBody(init.body);
            if (patched) {
                console.warn('[Fix19] fetch POST body patched:', url.substring(0, 100));
                init = Object.assign({}, init, {body: patched});
            }
        }

        const resp = await _origFetch(input, init);
        if (!isBrandUrl(url)) return resp;
        try {
            const ct = resp.headers.get('content-type') || '';
            if (!ct.includes('json')) return resp;
            const text = await resp.clone().text();
            const body = JSON.parse(text);
            injectNoBrand(body, 0);
            return new Response(JSON.stringify(body), {
                status: resp.status, statusText: resp.statusText, headers: resp.headers
            });
        } catch(e) { return resp; }
    };

    // ── XHR override ──────────────────────────────────────────────────────
    const _origOpen = XMLHttpRequest.prototype.open;
    const _origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(m, url) {
        this.__fix15url = String(url || '');
        this.__fix15method = String(m || 'GET').toUpperCase();
        return _origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        // Fix19: XHR POST ボディの brand_id を 0 に強制
        if (this.__fix15method === 'POST' && body && typeof body === 'string') {
            const patched = patchPostBody(body);
            if (patched) {
                console.warn('[Fix19] XHR POST body patched:', this.__fix15url.substring(0, 100));
                body = patched;
            }
        }
        if (this.__fix15url && isBrandUrl(this.__fix15url)) {
            const xhr = this;
            xhr.addEventListener('readystatechange', function() {
                if (xhr.readyState === 4 && !xhr.__fix15patched) {
                    xhr.__fix15patched = true;
                    try {
                        const ct = xhr.getResponseHeader('content-type') || '';
                        if (!ct.includes('json')) return;
                        const resp = JSON.parse(xhr.responseText);
                        injectNoBrand(resp, 0);
                        const p = JSON.stringify(resp);
                        Object.defineProperty(xhr, 'responseText',
                            { get: () => p, configurable: true });
                        Object.defineProperty(xhr, 'response',
                            { get: () => p, configurable: true });
                    } catch(e) {}
                }
            }, true);
        }
        return _origSend.call(this, body);
    };

    console.log('[Fix15+Fix19] brand/submit interceptor installed ✓');
    return 'installed';
})()
"""

    def _setup_brand_api_intercept(self):
        """Fix15: ページ内 JS fetch/XHR インターセプト（Fix14 Playwright route の代替）

        CDP既存タブでは page.route() が機能しないため、
        page.evaluate() で JS を直接注入してブランドAPIレスポンスを改変する。

        注入タイミング: start() 時点（接続直後）
        効果: カテゴリ変更時にブランドAPIが返す brand_list に "No Brand" を追加し、
              Vue が mandatory brand を強制しても "No Brand" が選択肢として残る。
        """
        try:
            result = self._page.evaluate(self._FIX15_JS)
            logger.info(f"[Fix15] ブランドJS interceptor 注入: {result}")
        except Exception as _se:
            logger.warning(f"[Fix15] JS注入失敗: {_se}")

    def _reinstall_brand_intercept(self):
        """Fix15: カテゴリ選択後など要所で再インストール（SPA遷移対応）"""
        try:
            result = self._page.evaluate(self._FIX15_JS)
            logger.debug(f"[Fix15] 再インストール: {result}")
        except Exception:
            pass

    # ── Fix17: Service Worker ブロック + page.route() ブランドAPIインターセプト ─────
    _FIX17_SW_BLOCK_JS = """
(function() {
    if (window.__fix17SwBlocked) return;
    window.__fix17SwBlocked = true;
    if (navigator.serviceWorker) {
        const _origReg = navigator.serviceWorker.register.bind(navigator.serviceWorker);
        navigator.serviceWorker.register = async function(scriptURL, options) {
            console.log('[Fix17] navigator.serviceWorker.register BLOCKED:', scriptURL);
            // Return a fake registration object
            return {
                scope: '/',
                updateViaCache: 'none',
                active: null, installing: null, waiting: null,
                addEventListener: () => {}, removeEventListener: () => {},
                dispatchEvent: () => false,
                unregister: async () => true,
                update: async () => undefined,
                onupdatefound: null,
            };
        };
        console.log('[Fix17] SW.register() hook installed ✓');
    }
})();
"""

    @staticmethod
    def _py_inject_no_brand(obj: dict, depth: int = 0) -> None:
        """Fix17: API レスポンス dict に "No Brand" エントリを注入し、全ブランドの is_mandatory を 0 に設定"""
        if not obj or depth > 8 or not isinstance(obj, dict):
            return
        # Use module-level helper to avoid class-name lookup issues in @staticmethod
        _inject_no_brand_recursive(obj, depth)

    def _unregister_service_workers(self) -> None:
        """Fix17: 現在のページで登録済みのService Workerを全てunregister"""
        try:
            n = self._page.evaluate("""
                async () => {
                    if (!navigator.serviceWorker) return 0;
                    const regs = await navigator.serviceWorker.getRegistrations();
                    await Promise.all(regs.map(r => r.unregister()));
                    return regs.length;
                }
            """)
            if n > 0:
                logger.info(f"[Fix17] Service Worker unregistered: {n}件")
            else:
                logger.debug("[Fix17] Service Worker: 登録なし（既にクリーン）")
        except Exception as _ue:
            logger.warning(f"[Fix17] SW unregister失敗: {_ue}")

    @staticmethod
    def _modify_brand_list(body: dict) -> None:
        """Fix18: get_brand_list レスポンスを改変。
        - brand_list 配列の全ブランドを is_mandatory=0 に
        - No Brand (brand_id=0) を先頭に注入
        - mandatory_brand_id / default_brand_id をクリア
        """
        # brand_list が data.brand_list にある場合 (最も一般的な構造)
        _inject_no_brand_recursive(body)

        # トップレベルおよびdataのmandatory_brand_id/default_brand_idをゼロ化
        for _obj in [body, body.get('data', {}), body.get('result', {})]:
            if not isinstance(_obj, dict):
                continue
            for _k in list(_obj.keys()):
                if any(x in _k.lower() for x in ('mandatory_brand', 'default_brand',
                                                   'required_brand', 'recommend_brand_id')):
                    _v = _obj[_k]
                    _obj[_k] = 0 if isinstance(_v, (int, float)) else ''

    @staticmethod
    def _modify_attribute_tree(body: dict) -> None:
        """Fix18: get_attribute_tree レスポンスを改変。
        - attr_name に 'brand' を含む属性の mandatory / default_value をクリア
        - brand_id / brand_name 等のフィールドをゼロ化
        """
        _inject_no_brand_recursive(body)

        # 再帰的に attribute 配列を探して brand 属性を optional 化
        def _fix_attrs(node, depth=0):
            if not node or depth > 10 or not isinstance(node, dict):
                return
            for _ak in ('attributes', 'attribute_list', 'attributeList', 'attrs',
                        'attr_list', 'data', 'result', 'response', 'children'):
                _val = node.get(_ak)
                if isinstance(_val, list):
                    for _item in _val:
                        if not isinstance(_item, dict):
                            continue
                        _name = str(_item.get('attr_name', _item.get('name',
                                    _item.get('attribute_name', '')))).lower()
                        if 'brand' in _name:
                            # brand属性を optional に
                            for _mf in ('is_mandatory', 'isMandatory', 'mandatory',
                                        'required', 'is_required'):
                                if _mf in _item:
                                    _item[_mf] = 0
                            # デフォルト値・推奨値をクリア
                            for _df in ('default_value', 'defaultValue', 'value',
                                        'recommended_value', 'recommend_value_id',
                                        'value_id', 'valueId', 'pre_selected_value_id'):
                                if _df in _item:
                                    _v = _item[_df]
                                    _item[_df] = (0 if isinstance(_v, (int, float))
                                                  else ([] if isinstance(_v, list) else ''))
                            # brandIdが直接フィールドにある場合もクリア
                            for _bf in ('brand_id', 'brandId', 'brand_name', 'brandName'):
                                if _bf in _item:
                                    _v = _item[_bf]
                                    _item[_bf] = 0 if isinstance(_v, (int, float)) else ''
                            logger.info(f"[Fix18] attribute_tree brand属性 optional化: {_name}")
                        else:
                            _fix_attrs(_item, depth + 1)
                elif isinstance(_val, dict):
                    _fix_attrs(_val, depth + 1)
            for _k, _v in node.items():
                if _k not in ('attributes', 'attribute_list', 'attributeList', 'attrs',
                              'attr_list', 'data', 'result', 'response', 'children'):
                    if isinstance(_v, dict):
                        _fix_attrs(_v, depth + 1)

        _fix_attrs(body)

    @staticmethod
    def _modify_brand_license_list(body: dict) -> None:
        """Fix32: get_brand_license_list の結果に関わらず偽ライセンスを先頭に注入。
        目的: Pelican 等の実ライセンスリストがある場合も "No License Required" を選択可能にする。
        Fix19 が POST から brand_license_id を削除するため、サーバーには影響しない。
        注入する偽ライセンス: id=99999, name="No License Required" (先頭に配置)
        """
        _fake = {
            # Fix34: EDS Select の valueKey が "value"/"id"/"license_id" のどれでも動くよう全部入れる
            "license_id": 99999, "id": 99999, "value": 99999, "key": 99999,
            "name": "No License Required", "license_name": "No License Required",
            "label": "No License Required",
            "license_type": 0, "type": 0,
        }
        data = body.get('data', None)
        if not isinstance(data, dict):
            return
        # Fix32: リストの有無に関わらず常に先頭へ偽ライセンスを注入（重複除去してから prepend）
        for _k in ('brand_license_list', 'license_list', 'list', 'licenses'):
            if _k in data:
                lst = data[_k] if isinstance(data[_k], list) else []
                # 既存の "No License Required" エントリを除去（重複防止）
                lst = [x for x in lst if str(x.get('name', '')) != 'No License Required']
                lst.insert(0, _fake)  # 先頭に配置 → opts[0] == "No License Required"
                data[_k] = lst
                logger.info(f"[Fix32] brand_license_list 先頭に偽ライセンス注入 (key={_k}, total={len(lst)})")
                return
        # フィールドが全くない場合: 実際のキーをDIAGして list を追加
        _actual_keys = list(data.keys())[:15]
        logger.info(f"[Fix32-DIAG] data の実際のキー: {_actual_keys}")
        data['list'] = [_fake]
        data['brand_license_list'] = [_fake]
        logger.info("[Fix32] brand_license_list に偽ライセンス注入 (new key)")

    @staticmethod
    def _modify_channel_info(body: dict) -> None:
        """Fix59: get_product_channel_info の応答をログしてMPSKUチャンネルを無効化する。
        目的: Vue フォームが MPSKU チャンネルを有効と見なさないようにして
              create_product_info リクエストに MPSKU フラグが付かないようにする。
        """
        import json as _json
        _raw = _json.dumps(body)[:600]
        logger.info(f"[Fix59] get_product_channel_info response: {_raw}")
        # MPSKU/cross-border 関連フィールドを無効化
        _mpsku_kws = ('mpsku', 'cbsc', 'cross_border', 'overseas', 'is_cb', 'enable_mpsku',
                      'channel_type', 'cross_listing', 'cb_product')
        def _disable_mpsku_recursive(obj, depth=0):
            if depth > 6:
                return
            if isinstance(obj, dict):
                for k, v in list(obj.items()):
                    if any(kw in k.lower() for kw in _mpsku_kws):
                        if isinstance(v, bool) and v:
                            logger.info(f"[Fix59] {k}=True → False (depth={depth})")
                            obj[k] = False
                        elif isinstance(v, int) and v not in (0, -1):
                            logger.info(f"[Fix59] {k}={v} → 0 (depth={depth})")
                            obj[k] = 0
                    _disable_mpsku_recursive(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    _disable_mpsku_recursive(item, depth + 1)
        _disable_mpsku_recursive(body)

    def _setup_fix17_route(self) -> None:
        """Fix17+Fix18: page.route() でブランドAPI群をインターセプトし No Brand を注入。
        対象: get_recommend_brand, get_recommend_attribute, get_content_filling_suggestion,
              get_brand_list (Fix18), get_attribute_tree (Fix18)
        """

        def _make_diag_handler(label: str, mutate: bool = True,
                               extra_mutator=None):
            """指定ラベルのDIAGハンドラを生成。mutate=Trueなら注入も行う。"""
            def _handler(route, request):
                try:
                    response = route.fetch()
                    try:
                        body = response.json()
                    except Exception:
                        route.fulfill(
                            status=response.status,
                            headers=dict(response.headers),
                            body=response.body(),
                        )
                        return
                    # DIAG: 生レスポンスをダンプ（brand強制フィールド特定用）
                    logger.info(f"[Fix17-DIAG:{label}] {json.dumps(body, ensure_ascii=False)[:5000]}")
                    if mutate:
                        _inject_no_brand_recursive(body)
                        _inject_brand_attribute_optional(body)
                    if extra_mutator:
                        extra_mutator(body)
                    hdrs = {
                        k: v for k, v in response.headers.items()
                        if k.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')
                    }
                    route.fulfill(
                        status=response.status,
                        headers=hdrs,
                        body=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                        content_type='application/json; charset=utf-8',
                    )
                    logger.info(f"[Fix17] ✅ 改変成功 [{label}]: {request.url[:100]}")
                except Exception as _re:
                    logger.warning(f"[Fix17] handler失敗[{label}] → continue: {_re}")
                    try:
                        route.continue_()
                    except Exception:
                        pass
            return _handler

        try:
            _targets = [
                # Fix17 originals
                ("**/get_recommend_brand**",           "rec_brand",    True,  None),
                ("**/get_recommend_attribute**",        "rec_attr",     True,  None),
                ("**/get_content_filling_suggestion**", "content_fill", True,  None),
                # Fix18: the real brand-setting APIs
                ("**/get_brand_list**",     "brand_list",   True,
                 ShopeeBrowser._modify_brand_list),
                ("**/get_attribute_tree**", "attr_tree",    True,
                 ShopeeBrowser._modify_attribute_tree),
                # Fix19b: MPSKU catalog matching API — DIAGのみ(mutate=False)で構造確認
                ("**/check_mpsku_for_edit**", "mpsku_check", False, None),
                # Fix59: get_product_channel_info をログ + MPSKUチャンネル無効化を試みる
                ("**/get_product_channel_info**", "channel_info", True,
                 ShopeeBrowser._modify_channel_info),
                # Fix24: brand_license_list に偽ライセンスを注入して Vue バリデーション通過
                # Fix19 が POST から brand_license_id を削除するのでサーバーには届かない
                ("**/get_brand_license_list**", "brand_lic",  True,
                 ShopeeBrowser._modify_brand_license_list),
            ]
            for _pat, _lbl, _mut, _extra in _targets:
                try:
                    self._page.unroute(_pat)
                except Exception:
                    pass
                self._page.route(_pat, _make_diag_handler(_lbl, _mut, _extra))
            logger.info("[Fix17+Fix18] page.route() ブランドAPI interceptor 設置 ✓")

            # Fix19: 商品出品API の POST ボディを傍受して brand_id=0 に強制
            def _fix19_submit_handler(route, request):
                if request.method != 'POST':
                    try:
                        route.continue_()
                    except Exception:
                        pass
                    return
                try:
                    raw = request.post_data or ''
                    body = json.loads(raw) if raw else {}
                    patched = False
                    is_create = 'create_product_info' in request.url

                    # Fix53: create_product_info のリクエストボディ構造をログ出力 + MPSKU フィールド削除
                    if is_create:
                        _top_keys = list(body.keys())[:20] if isinstance(body, dict) else '?'
                        _brand_top = body.get('brand_id', body.get('brandId', 'NOT_FOUND'))
                        logger.info(f"[Fix53] create_product_info body keys: {_top_keys}")
                        logger.info(f"[Fix53] top-level brand_id: {_brand_top}")
                        # Dump nested keys one level deep to find brand_id location and MPSKU fields
                        for _nk in ('item_info', 'product_info', 'data', 'item', 'product', 'basic', 'info'):
                            if _nk in body and isinstance(body.get(_nk), dict):
                                _pi = body[_nk]
                                _nb = _pi.get('brand_id', _pi.get('brandId', 'NOT_FOUND'))
                                logger.info(f"[Fix53] body[{_nk}].brand_id: {_nb}")
                                logger.info(f"[Fix53] body[{_nk}] ALL keys ({len(_pi)}): {sorted(_pi.keys())}")
                                # Find and log MPSKU-related fields
                                _mpsku_kws = ('mpsku', 'cbsc', 'cross_border', 'overseas', 'cb_', 'sync_', 'mp_sku', 'is_mp')
                                _mpsku_fields = {k: v for k, v in _pi.items() if any(kw in k.lower() for kw in _mpsku_kws)}
                                logger.info(f"[Fix53] MPSKU-related fields in body[{_nk}]: {_mpsku_fields}")
                                # Fix54: Remove/clear MPSKU fields to prevent MPSKU lock error
                                _removed_mpsku = []
                                for _mk in list(_pi.keys()):
                                    if any(kw in _mk.lower() for kw in _mpsku_kws):
                                        _old_val = _pi[_mk]
                                        # Try to zero/false it rather than delete (safer for required fields)
                                        if isinstance(_old_val, bool):
                                            _pi[_mk] = False
                                        elif isinstance(_old_val, int):
                                            _pi[_mk] = 0
                                        elif isinstance(_old_val, str) and _old_val:
                                            _pi[_mk] = ''
                                        elif isinstance(_old_val, list):
                                            _pi[_mk] = []
                                        elif isinstance(_old_val, dict):
                                            _pi[_mk] = {}
                                        _removed_mpsku.append(f"{_mk}:{_old_val}→{_pi[_mk]}")
                                        patched = True
                                if _removed_mpsku:
                                    logger.info(f"[Fix54] MPSKU fields patched: {_removed_mpsku}")

                    def _patch_obj(o, depth=0):
                        nonlocal patched
                        if not isinstance(o, dict) or depth > 8:
                            return
                        for k in ('brand_id', 'brandId'):
                            if k in o and isinstance(o[k], int) and o[k] != 0:
                                logger.info(f"[Fix19] POST submit {k}={o[k]} → 0 (depth={depth})")
                                o[k] = 0
                                patched = True
                        for k in ('brand_license_id', 'brandLicenseId', 'license_id'):
                            if k in o:
                                del o[k]
                                patched = True
                        # Fix58: parent_sku が非空だと MPSKU 製品として分類される → 空文字に強制
                        if 'parent_sku' in o and o['parent_sku']:
                            logger.info(f"[Fix58] parent_sku='{str(o['parent_sku'])[:60]}' → '' (MPSKU防止)")
                            o['parent_sku'] = ''
                            patched = True
                        # Recurse into ALL dict values (not just known keys) for create_product_info
                        for nested_k, nested_v in list(o.items()):
                            if isinstance(nested_v, dict):
                                _patch_obj(nested_v, depth + 1)
                            elif isinstance(nested_v, list):
                                for item in nested_v:
                                    _patch_obj(item, depth + 1)
                    _patch_obj(body)

                    # Fix53: log full values for MPSKU-diagnosis fields
                    if is_create and isinstance(body, dict):
                        _pi_vals = body.get('product_info', {}) or {}
                        logger.info(f"[Fix53] top-level is_draft value: {body.get('is_draft', 'NOT_FOUND')}")
                        logger.info(f"[Fix53] product_info.unlisted: {_pi_vals.get('unlisted', 'NOT_FOUND')}")
                        logger.info(f"[Fix53] product_info.scheduled_publish_time: {_pi_vals.get('scheduled_publish_time', 'NOT_FOUND')}")
                        logger.info(f"[Fix53] product_info.parent_sku: {str(_pi_vals.get('parent_sku', 'NOT_FOUND'))[:100]}")
                        _lc = _pi_vals.get('logistics_channels', [])
                        logger.info(f"[Fix53] product_info.logistics_channels (first 2): {str(_lc[:2])[:300]}")

                    # Fix55/Fix56/Fix57: create_product_info MPSKU上限エラー回避
                    # MPSKU エラー (1000100256): shop の公開 MPSKU 製品上限超過
                    # Fix57: 段階的フォールバック戦略:
                    #   1. scheduled_publish_time = now+120s (スケジュール公開でMPSKU上限回避を試みる)
                    #   2. unlisted=True (非公開製品作成でMPSKU上限回避を試みる)
                    _MPSKU_ERR = 1000100256

                    def _extract_item_id(d):
                        """レスポンスから item_id を抽出"""
                        if not isinstance(d, dict):
                            return None
                        return (d.get('data') or {}).get('item_id') or \
                               d.get('item_id') or \
                               ((d.get('data') or {}).get('item') or {}).get('item_id') or \
                               (d.get('response') or {}).get('item_id')

                    if is_create:
                        # まず create_product_info (通常公開) を試みる
                        response = route.fetch(post_data=json.dumps(body) if patched else None)
                        try:
                            _first_resp = response.json()
                            _first_code = _first_resp.get('code', _first_resp.get('error'))
                        except Exception:
                            _first_code = None
                            _first_resp = {}

                        if _first_code == _MPSKU_ERR:
                            import time as _time
                            logger.info(f"[Fix57] MPSKU limit hit (code={_MPSKU_ERR}) — trying fallback strategies")

                            # --- Fallback 1: scheduled_publish_time = now+120s ---
                            # スケジュール公開製品はMPSKU上限外の可能性
                            _f57_body1 = dict(body)
                            _f57_pi1 = dict(_f57_body1.get('product_info', {}))
                            _f57_pi1['scheduled_publish_time'] = int(_time.time()) + 120
                            _f57_pi1['unlisted'] = False  # 明示的にunlisted=False
                            _f57_body1['product_info'] = _f57_pi1
                            _f57_body1.pop('is_draft', None)  # is_draft削除
                            logger.info(f"[Fix57] Fallback1: scheduled_publish_time={_f57_pi1['scheduled_publish_time']}")
                            try:
                                _f57_r1 = route.fetch(post_data=json.dumps(_f57_body1))
                                _f57_c1 = _f57_r1.json()
                                _f57_code1 = _f57_c1.get('code', _f57_c1.get('error', '?'))
                                _f57_msg1  = str(_f57_c1.get('message', _f57_c1.get('msg', '')))[:200]
                                logger.info(f"[Fix57] Fallback1 result: HTTP={_f57_r1.status} code={_f57_code1} msg={_f57_msg1}")
                                _f57_id1 = _extract_item_id(_f57_c1)
                                if _f57_code1 == 0 and _f57_id1:
                                    logger.info(f"[Fix57] ✅ Fallback1 SUCCESS! item_id={_f57_id1} (scheduled+120s)")
                                    _synth = json.dumps({'code': 0, 'message': 'success', 'data': {'item_id': _f57_id1}})
                                    route.fulfill(status=200, content_type='application/json', body=_synth)
                                    return
                                else:
                                    logger.warning(f"[Fix57] Fallback1 failed (code={_f57_code1}) — trying Fallback2")
                                    response = _f57_r1  # default to showing this error
                            except Exception as _e1:
                                logger.warning(f"[Fix57] Fallback1 error: {_e1}")

                            # --- Fallback 2: unlisted=True ---
                            # 非公開製品はMPSKU公開上限外の可能性
                            _f57_body2 = dict(body)
                            _f57_pi2 = dict(_f57_body2.get('product_info', {}))
                            _f57_pi2['unlisted'] = True
                            _f57_pi2.pop('scheduled_publish_time', None)  # scheduled_publish_time削除
                            _f57_body2['product_info'] = _f57_pi2
                            _f57_body2.pop('is_draft', None)
                            logger.info(f"[Fix57] Fallback2: unlisted=True")
                            try:
                                _f57_r2 = route.fetch(post_data=json.dumps(_f57_body2))
                                _f57_c2 = _f57_r2.json()
                                _f57_code2 = _f57_c2.get('code', _f57_c2.get('error', '?'))
                                _f57_msg2  = str(_f57_c2.get('message', _f57_c2.get('msg', '')))[:200]
                                logger.info(f"[Fix57] Fallback2 result: HTTP={_f57_r2.status} code={_f57_code2} msg={_f57_msg2}")
                                _f57_id2 = _extract_item_id(_f57_c2)
                                if _f57_code2 == 0 and _f57_id2:
                                    logger.info(f"[Fix57] ✅ Fallback2 SUCCESS! item_id={_f57_id2} (unlisted=True)")
                                    _synth = json.dumps({'code': 0, 'message': 'success', 'data': {'item_id': _f57_id2}})
                                    route.fulfill(status=200, content_type='application/json', body=_synth)
                                    return
                                else:
                                    logger.warning(f"[Fix57] Fallback2 also failed (code={_f57_code2}) — no more fallbacks")
                                    response = _f57_r2
                            except Exception as _e2:
                                logger.warning(f"[Fix57] Fallback2 error: {_e2}")

                            # --- Fallback 3: logistics_channels=[] ---
                            # logisticsなしだとMPSKU分類されない可能性 (下書き/不完全として保存)
                            _f57_body3 = dict(body)
                            _f57_pi3 = dict(_f57_body3.get('product_info', {}))
                            _f57_pi3['logistics_channels'] = []
                            _f57_pi3.pop('unlisted', None)
                            _f57_pi3.pop('scheduled_publish_time', None)
                            _f57_body3['product_info'] = _f57_pi3
                            _f57_body3.pop('is_draft', None)
                            logger.info(f"[Fix57] Fallback3: logistics_channels=[]")
                            try:
                                _f57_r3 = route.fetch(post_data=json.dumps(_f57_body3))
                                _f57_c3 = _f57_r3.json()
                                _f57_code3 = _f57_c3.get('code', _f57_c3.get('error', '?'))
                                _f57_msg3  = str(_f57_c3.get('message', _f57_c3.get('msg', '')))[:300]
                                logger.info(f"[Fix57] Fallback3 result: HTTP={_f57_r3.status} code={_f57_code3} msg={_f57_msg3}")
                                _f57_id3 = _extract_item_id(_f57_c3)
                                if _f57_code3 == 0 and _f57_id3:
                                    logger.info(f"[Fix57] ✅ Fallback3 SUCCESS! item_id={_f57_id3} (logistics=[])")
                                    _synth = json.dumps({'code': 0, 'message': 'success', 'data': {'item_id': _f57_id3}})
                                    route.fulfill(status=200, content_type='application/json', body=_synth)
                                    return
                                else:
                                    logger.warning(f"[Fix57] Fallback3 also failed — no more fallbacks")
                                    response = _f57_r3
                            except Exception as _e3:
                                logger.warning(f"[Fix57] Fallback3 error: {_e3}")
                    else:
                        response = route.fetch(post_data=json.dumps(body) if patched else None)

                    route.fulfill(response=response)
                    # Fix21: レスポンスの code/message を記録してサーバー側バリデーション確認
                    try:
                        _resp_body = response.json()
                        _code = _resp_body.get('code', _resp_body.get('error', '?'))
                        _msg  = str(_resp_body.get('message', _resp_body.get('msg', '')))[:200]
                        # Fix53: create_product_info では全レスポンスをダンプ
                        if is_create:
                            logger.info(f"[Fix53] create_product_info response: code={_code} msg={_msg}")
                            # Log any validation errors in the response
                            _errs = _resp_body.get('data', {})
                            if isinstance(_errs, dict):
                                logger.info(f"[Fix53] response data keys: {list(_errs.keys())[:20]}")
                        logger.info(f"[Fix19] submit intercept: patched={patched} code={_code} msg={_msg} url={request.url[:80]}")
                    except Exception:
                        logger.info(f"[Fix19] submit intercept: patched={patched} url={request.url[:80]}")
                except Exception as _fe:
                    logger.warning(f"[Fix19] submit handler失敗: {_fe}")
                    try:
                        route.continue_()
                    except Exception:
                        pass

            for _submit_pat in [
                '**/api/v3/product/add_product**',
                '**/api/v3/product/save_product**',
                '**/api/v3/listing-upload/submit**',
                '**/api/v3/listing-upload/save**',
                '**/api/v3/product/create_product_info**',  # Fix52: actual submit endpoint discovered in Run95
            ]:
                try:
                    self._page.unroute(_submit_pat)
                except Exception:
                    pass
                try:
                    self._page.route(_submit_pat, _fix19_submit_handler)
                    logger.info(f"[Fix19] submit route 設置: {_submit_pat}")
                except Exception as _sr:
                    logger.warning(f"[Fix19] submit route 設置失敗 {_submit_pat}: {_sr}")

        except Exception as _re:
            logger.warning(f"[Fix17] page.route()設置失敗: {_re}")

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

            # Fix15+Fix17: /product/new ナビゲーション後（JS状態クリア）に再インジェクト
            # page.goto() は全ページリロード → Fix15 window.fetch override が失われる → 再設定
            # Fix17 page.route() はそのまま維持されるが SW が再登録されている可能性あり
            self._unregister_service_workers()
            self._setup_brand_api_intercept()

            # /product/new がログインにリダイレクトされた場合: リロード前に再ログイン
            _f27_ran = False  # Fix28: Fix27実行フラグ
            if "login" in self._page.url:
                _f27_actual_url = self._page.url
                logger.warning(f"  [Fix27] セッション切れ検出 URL: {_f27_actual_url}")
                # Fix27: product/list/all は通るが product/new は失敗する場合
                # login() の _is_logged_in ショートカット（product/list/all 確認）では
                # product/new に必要なセッション鮮度が回復しない → 強制フルログイン
                logger.info("  [Fix27] 強制フルログイン（セッションチェックバイパス）")
                try:
                    self._page.goto(
                        f"{SHOPEE_SELLER_URL}/account/login",
                        timeout=30000,
                        wait_until="domcontentloaded",
                    )
                    _human_wait(2, 3)
                    try:
                        self._page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    _f27_login_url = self._page.url
                    if "login" not in _f27_login_url:
                        # account/login へ行ったらリダイレクトされた → セッション有効
                        logger.info(f"  [Fix27] account/login → リダイレクト先: {_f27_login_url}")
                    else:
                        # ログインフォームが表示 → 資格情報を入力してログイン実行
                        logger.info("  [Fix27] ログインフォーム表示 → 資格情報入力開始")
                        self._dismiss_modals()
                        _human_wait(1, 2)
                        if self._detect_captcha():
                            logger.error("  [Fix27] ❌ CAPTCHAが表示されています")
                            _notify_captcha()
                            self._session_error_count += 1
                            return None
                        input_sel = 'input[name="loginKey"], input[type="text"]'
                        self._page.wait_for_selector(input_sel, timeout=10000)
                        _human_wait(0.5, 1.0)
                        self._page.locator(input_sel).first.fill(SHOPEE_EMAIL)
                        _human_wait(0.5, 1.0)
                        self._page.locator('input[type="password"]').first.fill(SHOPEE_PASSWORD)
                        _human_wait(0.8, 1.5)
                        login_btn_sel = (
                            'button:has-text("LOG IN"), '
                            'button:has-text("Log In"), '
                            'button:has-text("เข้าสู่ระบบ"), '
                            'button[type="submit"]'
                        )
                        self._page.wait_for_selector(login_btn_sel, timeout=8000)
                        _human_wait(0.5, 1.0)
                        self._page.locator(login_btn_sel).first.click()
                        logger.info("  [Fix27] LOG IN ボタンクリック")
                        _human_wait(5, 8)
                        if self._detect_captcha():
                            logger.error("  [Fix27] ❌ ログイン後CAPTCHA")
                            _notify_captcha()
                            self._session_error_count += 1
                            return None
                        self._save_cookies()
                        logger.info(f"  [Fix27] ログイン後URL: {self._page.url}")
                except Exception as _f27_e:
                    logger.error(f"  [Fix27] 強制ログイン例外: {_f27_e}")
                    self._session_error_count += 1
                    if self._session_error_count >= MAX_CONSECUTIVE_SESSION_ERRORS:
                        raise LoginLoopError(
                            f"[Fix27] ログイン失敗が{self._session_error_count}回連続 — 自動中断"
                        )
                    return None

                # 強制ログイン後 product/new へ
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
                        f"  [Fix27] ❌ 強制ログイン後も /product/new でセッション切れ "
                        f"({self._session_error_count}/{MAX_CONSECUTIVE_SESSION_ERRORS})"
                        f" URL: {self._page.url}"
                    )
                    if self._session_error_count >= MAX_CONSECUTIVE_SESSION_ERRORS:
                        raise LoginLoopError(
                            f"[Fix27] ログイン→セッション切れのループを{self._session_error_count}回検出 — 自動中断"
                        )
                    return None
                # ログイン成功・セッション回復
                self._session_error_count = 0
                logger.info("  [Fix27] セッション回復 — product/new 到達成功")
                _f27_ran = True  # Fix28: リロードスキップフラグ

            # Fix28: page が閉じられていたら context から再取得
            if self._page.is_closed():
                logger.warning("  [Fix28] page が閉じられた → context から再取得")
                _f28_pages = [p for p in self._context.pages if not p.is_closed()]
                if _f28_pages:
                    self._page = _f28_pages[-1]
                    logger.info(f"  [Fix28] page 復元: {self._page.url}")
                else:
                    logger.error("  [Fix28] ❌ context に利用可能な page なし")
                    return None

            # Fix28: Fix27直後はgoto済み(fresh)なのでリロード不要
            if _f27_ran:
                logger.info("  [Fix28] Fix27直後 — reload スキップ（product/new fresh済み）")
                _human_wait(1, 2)
            else:
                # ページをリロードしてドラフト復元をクリア
                self._page.reload(wait_until="domcontentloaded", timeout=60000)
                try:
                    self._page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                _human_wait(3, 5)

            # Fix17: リロード後に残存SWをunregister（init_scriptでブロック済みだが belt-and-suspenders）
            self._unregister_service_workers()
            # Fix17: page.route() を再設置（page.goto/reload でクリアされた場合に備える）
            self._setup_fix17_route()

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

            # 商品名入力 (Fix26: selector を大幅拡張 + JS fallback)
            title_sel = (
                'input[placeholder*="Brand Name"], '
                'input[placeholder*="Key Features"], '
                'input[placeholder*="Product Name"], '
                'input[placeholder*="product name"], '
                'input[placeholder*="ชื่อสินค้า"], '
                'input[placeholder*="กรอกชื่อ"], '
                'input[placeholder*="Enter product"], '
                'input[placeholder*="Enter Product"], '
                'input[placeholder*="Tên sản phẩm"], '
                'label:has-text("Product Name") ~ div input, '
                'label:has-text("Product Name") + div input, '
                'label:has-text("ชื่อสินค้า") ~ div input, '
                'label:has-text("ชื่อสินค้า") + div input'
            )
            title_input = None
            try:
                self._page.wait_for_selector(title_sel, timeout=30000)
                _human_wait(0.5, 1.0)
                title_input = self._page.locator(title_sel).first
                logger.info("  [Fix26] 商品名 input 検出（既定selector）")
            except PlaywrightTimeout:
                # Fix26 fallback: 幅広い input を診断ログとともに再探索
                logger.warning("  [Fix26] 既定selectors タイムアウト → 診断 + 再探索")
                self._screenshot(f"debug_{mw_id}_title_timeout")
                try:
                    _diag = self._page.evaluate("""
                        () => {
                            const all = [...document.querySelectorAll('input')];
                            return all.map(el => ({
                                ph: el.placeholder || '',
                                type: el.type || '',
                                w: Math.round(el.getBoundingClientRect().width),
                                ml: el.maxLength,
                                vis: el.offsetParent !== null,
                            }));
                        }
                    """)
                    logger.info(f"  [Fix26] 全input診断: {_diag[:10]}")
                except Exception:
                    pass
                # 幅200px以上・maxLength≥50 の visible input を探す
                _f26_sel = (
                    'input[maxlength]:not([maxlength="1"]):not([maxlength="2"]):not([maxlength="10"])'
                )
                try:
                    self._page.wait_for_selector(_f26_sel, timeout=15000)
                    _candidates = self._page.locator(_f26_sel).all()
                    _visible = [c for c in _candidates if c.is_visible()]
                    if _visible:
                        title_input = _visible[0]
                        logger.info(f"  [Fix26] maxlength fallback で input 取得 ({len(_visible)}件候補)")
                    else:
                        raise Exception("visible input なし")
                except Exception as _f26_e:
                    inputs = self._page.locator("input:visible").all()
                    logger.error(f"[Fix26] 商品名入力欄が見つかりません（visible input数: {len(inputs)}）: {_f26_e}")
                    return None
            try:
                title_input.click()
                _human_wait(0.3, 0.5)
                title_input.fill(title)
                logger.info(f"  商品名入力完了: {title[:30]}")
            except Exception as _ti_e:
                logger.error(f"商品名入力エラー: {_ti_e}")
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
            # Pre-publish リカバリ経路でも参照できるよう self に退避
            self._last_weight_kg = weight_kg

            # カテゴリ選択は Basic Info タブ内（レンダリング後）で実行するため
            # ここでは定数だけ定義しておく
            CAT_BLOCKED = ["medical", "fda", "mom & baby", "stuffed toy", "sexual",
                           "wellness", "adult", "pharmaceutical", "health >",
                           "muslim", "hijab", "prayer", "baby >", "doll",
                           # ブランドライセンス必須カテゴリ（選択肢なし→必ず失敗）
                           # NOTE: "lighting" は "home & living > lighting" だけブロック
                           #   "cameras & drones > camera accessories > lighting" は安全（Apr16確認済）
                           "home & living > lighting",
                           "vehicles", "motorcycle", "automotive",
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
                           "collectible items > vehicle",
                           # Fix9: ブランド自動割当カテゴリ（No Brand クリックが Vue にリバートされる）
                           "camping", "knives", "survival kits", "outdoor recreation equipments",
                           # Run 36/37 確認: Shopee がブランドを強制するカテゴリ（early skip 有効）
                           # 3D Printers → Eazy Toner 強制（3Dプリンター本体のカテゴリ、3D印刷品はここじゃない）
                           "printers & scanners", "3d printers",
                           # Computers & Accessories → Thai tech ブランド強制
                           # ただし > Others は例外として許容（score で競わせる）
                           "computers & accessories > peripherals",
                           "computers & accessories > laptops",
                           "computers & accessories > tablets",
                           "computers & accessories > networking",
                           # Home Appliances → Lotus/Thai brands 強制
                           "small household appliances",  # Run 37 で Lotus 強制を確認
                           "home appliances",
                           # 教育・文具系 → Thai education brands 強制
                           "educational toys",
                           "school & office equipment",
                           # Collectible 強制ブランド（Huangdo/Domon等）
                           # Fix17: idol collectibles も Huangdo 強制と確認 (run53 Brand確認で判明)
                           "idol collectibles",           # Huangdo 強制 → ブロック (run53確認)
                           "collectible items > others",  # Run 36/37 で Huangdo/Domon 強制を確認
                           "collectible items > statues",
                           "collectible items > sport",  # Run 37 で Huangdo 強制確認
                           # Fix17: USB/Mobile 系は ACONATIC 強制 → ブロック
                           "usb & mobile lights",         # ACONATIC 強制 (run50/52/53確認)
                           "mobile & gadgets > accessories > usb",  # 上記と同グループ
                           # Run 37 で Brand License 必須と判明（新規追加）
                           "souvenirs",        # Hobbies & Collections > Souvenirs → ดอกหญ้าวิชาการ 強制
                           "men bags",         # Men Bags → Dapper 強制
                           "pet clothing",     # Pets > Pet Clothing → AG-SCIENCE 強制
                           "pet accessories",  # Pets > Pet Accessories → AG-SCIENCE 強制
                           "litter & toilet",  # Pets > Litter & Toilet → AG-SCIENCE 強制
                           ]
            # Fix36: "Spare Parts and Accessories for Vehicles" 全体がこのセラーアカウントで
            # 制限済み（"Selected category is not supported" エラー確認 run78）→ WHITELISTを空に。
            # "vehicles" キーワードが全サブカテゴリをブロックするため追加対処不要。
            CAT_WHITELIST: list[str] = []
            CAT_PREF = {
                # === Apr16 確認済み安全カテゴリ（最優先） ===
                # "idol collectibles": 5,  # Fix17: Huangdo 強制と判明 → CAT_BLOCKED に移動
                "vehicular tools": 5,     # Fix35: ツール系は No Brand 許容（正しいカテゴリ）
                "camera accessories": 4,  # Cameras & Drones > Camera Accessories > Lighting → 安全確認済
                "photography & printing": 4,  # Tickets, Vouchers & Services > Services → 安全確認済
                # === 一般的に安全なカテゴリ ===
                "tools": 3,
                "diy": 4,          # DIYは最優先（No Brand 許可が多い）
                "hobbies": 2,      # Run36で collectible系の BL 多発 → 下げ
                "collectible": 2,  # idol collectibles は安全→ 少し上げ（危険な sub-cat はブロック済）
                "arts": 3,
                "craft": 3,
                "sport": 2,
                "home & living": 2,  # 安全な汎用カテゴリ → 優先度上げ
                "home decor": 3,
                "stationery": 1,
                "electronics": -1, # Thai tech brands が多い → 下げ
                "computers": -2,   # Computers全般 → Thai brands 強制が多い → 大幅下げ
                "pets": -3,        # Run37確認: Pets全般 AG-SCIENCE 強制 → 大幅下げ
                # Fix9: Others を正の値に変更（ブランド自動割当を避けるため積極選択）
                # 理由: 特定カテゴリ選択後に Vue がブランドを強制割当し No Brand クリックを無効化する
                "others": 2,
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
                    # Fix17: TOCTOU修正 — スコアリングとマーキングを1回のevaluateで行う
                    try:
                        import json as _json
                        _cat_blocked_js = _json.dumps(CAT_BLOCKED)
                        _cat_pref_js = _json.dumps(CAT_PREF)
                        # 1回のevaluateでスコアリング+マーキングを原子的に実行 (TOCTOU修正)
                        cat_select_result = self._page.evaluate(f"""
                            () => {{
                                const CAT_BLOCKED = {_cat_blocked_js};
                                const CAT_PREF    = {_cat_pref_js};

                                function catScore(txt) {{
                                    let s = 0;
                                    for (const [kw, w] of Object.entries(CAT_PREF)) {{
                                        if (txt.includes(kw)) s += w;
                                    }}
                                    return s;
                                }}

                                // sparkle icon を起点に Recommended Categories セクションを特定
                                const sparkle = document.querySelector('[class*="sparkle"]');
                                if (!sparkle) return {{items: [], marked: null}};
                                let container = sparkle.parentElement;
                                for (let lvl = 0; lvl < 8 && container && container !== document.body;
                                     lvl++, container = container.parentElement) {{
                                    const elems = [];
                                    for (const el of container.querySelectorAll('*')) {{
                                        if (el === sparkle || el.contains(sparkle)) continue;
                                        if (el.children.length > 2) continue;
                                        const txt = el.textContent.trim();
                                        if (!txt.includes(' > ') || txt.length > 150) continue;
                                        const rect = el.getBoundingClientRect();
                                        if (!rect || rect.width < 50 || rect.height < 8) continue;
                                        elems.push({{el, txt}});
                                    }}
                                    if (elems.length === 0) continue;

                                    // 同一テキストの重複を除去（同じカテゴリが2個出ることがある）
                                    const seen = new Set();
                                    const unique = [];
                                    for (const item of elems) {{
                                        const key = item.txt.toLowerCase().trim();
                                        if (!seen.has(key)) {{ seen.add(key); unique.push(item); }}
                                    }}

                                    // スコアリング（ブロックリストチェック + 優先度）
                                    // Fix36: Spare Parts/Vehicles は seller アカウント制限確認 → whitelist 空
                                    const CAT_WHITELIST = [];
                                    let bestEl = null, bestTxt = '', bestScore = -Infinity;
                                    const itemsInfo = [];
                                    for (const {{el, txt}} of unique) {{
                                        const tl = txt.toLowerCase();
                                        const whitelisted = CAT_WHITELIST.some(kw => tl.includes(kw));
                                        const blocked = !whitelisted && CAT_BLOCKED.some(kw => tl.includes(kw));
                                        const score = blocked ? -9999 : catScore(tl);
                                        itemsInfo.push({{text: txt, score, blocked}});
                                        if (score > bestScore) {{
                                            bestScore = score;
                                            bestEl = el;
                                            bestTxt = txt;
                                        }}
                                    }}

                                    // 最良カテゴリをマーク（Playwright クリック用）
                                    document.querySelectorAll('[data-cat-sel]').forEach(
                                        e => e.removeAttribute('data-cat-sel'));
                                    if (bestEl && bestScore > -9999) {{
                                        const row = bestEl.closest('li, [class*="item"], [class*="row"]')
                                                    || bestEl.parentElement;
                                        row.setAttribute('data-cat-sel', 'target');
                                    }}
                                    return {{items: itemsInfo, marked: bestScore > -9999 ? bestTxt : null}};
                                }}
                                return {{items: [], marked: null}};
                            }}
                        """)
                        reco_info = cat_select_result.get('items', [])
                        mark_result = cat_select_result.get('marked')
                        logger.info(f"  推奨カテゴリ一覧: {[(r['text'][:60], r['score']) for r in reco_info]}")

                        _do_pencil = False  # 推奨カテゴリ失敗 or なし → pencil edit フォールバック

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
                        # === DIAG: カテゴリ確定直後からネットワーク監視開始 ===
                        _diag_responses = []
                        def _diag_on_resp(resp):
                            u = resp.url
                            if any(kw in u.lower() for kw in
                                   ['brand', 'recommend', 'category', 'attribute',
                                    'fill', 'suggest', 'product', 'api/v']):
                                _diag_responses.append(u)
                        try:
                            self._page.on('response', _diag_on_resp)
                        except Exception:
                            pass
                        # === END DIAG SETUP ===
                        _human_wait(3.0, 4.0)  # Vue 再描画 + カテゴリ確定待ち（長めに）
                        # Fix15: カテゴリ確定後にブランドAPIインターセプトを再インストール
                        # （SPA ページ遷移でスクリプトが失われる場合への対策）
                        self._reinstall_brand_intercept()
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
                        if not mark_result:
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
                                        _rc_lower = _rc.lower()
                                        # Fix35: ホワイトリスト優先（vehicular tools 等）
                                        _whitelisted = any(kw in _rc_lower for kw in CAT_WHITELIST)
                                        if not _whitelisted and any(kw in _rc_lower for kw in CAT_BLOCKED):
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
                                    # Fix37: ブロック済みカテゴリのパスは使わない（固定候補リストにフォールバック）
                                    # 旧: ブロック済みカテゴリを nav_parts に使って blocked カテゴリにナビしてしまっていた
                                    # 新: 非ブロックカテゴリのみ nav_parts に使用。全ブロック時は固定リストへ。
                                    _nav_parts = None
                                    for _rc in (_visible_cat_paths or []):
                                        _rc_lower = _rc.lower()
                                        _whitelisted = any(kw in _rc_lower for kw in CAT_WHITELIST)
                                        if not _whitelisted and any(kw in _rc_lower for kw in CAT_BLOCKED):
                                            # Fix37: blocked → skip（固定候補リストへフォールバック）
                                            logger.info(f"  動的ナビ: ブロック済みスキップ ({_rc[:55]})")
                                            continue
                                        # 非ブロックカテゴリが見つかった → nav_parts に使う
                                        parts = [p.strip() for p in _rc.split(' > ')]
                                        if len(parts) >= 2:
                                            _nav_parts = parts
                                            logger.info(f"  動的ナビパス (非ブロック): {' > '.join(_nav_parts)}")
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
                                        # Fix17 (run54): 優先順位を Home & Living > Home Decor に変更
                                        # 理由: Idol Collectibles/Collectible Items は Huangdo 強制 (run53確認)
                                        #       Hobbies 系は全て Huangdo/Domon 強制のためスキップ
                                        # 安全な経路: Home & Living > Home Decor (> Others)
                                        _l2_selected_txt = None
                                        for level, candidates in [
                                            (1, ["Cameras & Drones",           # 最優先: Camera Accessories > Lighting = Apr16確認済み安全
                                                 "กล้องและโดรน",                # Thai: Cameras & Drones
                                                 "Home & Living",              # 次善: Decoration 試みる (Home & Livingは全般Oonew強制)
                                                 "บ้านและชีวิตประจำวัน",
                                                 "Tools",
                                                 "Sports",
                                                 "งานอดิเรก", "Hobbies & Collections", "Hobbies",
                                                 "งานอดิเรกและของสะสม"]),
                                            (2, ["Camera Accessories",         # Cameras & Drones > Camera Accessories (安全)
                                                 "Accessories",                 # 別名かも
                                                 "Photography Accessories",
                                                 "Decoration",                 # Home & Living > Decoration
                                                 "Tools & Home Improvement",
                                                 "Kitchenware",
                                                 "Bedding",
                                                 "DIY", "DIY & Craft Supplies",
                                                 "Art & Craft", "Arts & Crafts",
                                                 "Hobby Supplies",
                                                 # Gardening → brand=Oonew 強制 (run55確認)
                                                 "Gardening", "Garden Supplies",
                                                 ]),
                                            (3, ["Lighting",                   # Camera Accessories > Lighting = Apr16確認済み安全
                                                 "Others",                     # その他の安全フォールバック
                                                 "Decorative Accents",
                                                 "Wall Art & Decor",
                                                 "Cookware",
                                                 ]),
                                        ]:
                                            level_clicked = False
                                            for txt in candidates:
                                                if level_clicked:
                                                    break
                                                if level == 3:
                                                    # L3 診断: スクリーンショット + 全リスト出力
                                                    try:
                                                        _diag_ss = f"/Users/shoichionizuka/sellersprite-lp-/3d-shopee-bot/errors/diag_L3_{int(time.time())}.png"
                                                        self._page.screenshot(path=_diag_ss)
                                                        logger.info(f"  [DIAG] L3スクリーンショット: {_diag_ss}")
                                                        _all_li = self._page.evaluate("""
                                                            () => {
                                                                const all = Array.from(document.querySelectorAll('li'));
                                                                return all.map(el => ({
                                                                    txt: el.innerText?.trim()?.substring(0,60),
                                                                    cls: el.className?.substring(0,60),
                                                                    vis: el.offsetParent !== null
                                                                })).filter(x => x.txt && x.vis).slice(0, 40);
                                                            }
                                                        """)
                                                        logger.info(f"  [DIAG] 可視 li: {[x['txt'] for x in _all_li[:20]]}")
                                                    except Exception as _de:
                                                        logger.debug(f"  [DIAG] エラー: {_de}")
                                                    # JS で "Idol Collectibles" を探してクリック（スクロールも試みる）
                                                    try:
                                                        _js_clicked = self._page.evaluate("""
                                                            (searchTxt) => {
                                                                // モーダル内の全 li を検索
                                                                const candidates = Array.from(document.querySelectorAll('li, [role="option"]'));
                                                                for (const el of candidates) {
                                                                    const t = el.innerText?.trim();
                                                                    if (t === searchTxt) {
                                                                        el.scrollIntoView({block:'center'});
                                                                        el.click();
                                                                        return {ok: true, txt: t};
                                                                    }
                                                                }
                                                                // 部分一致も試す
                                                                for (const el of candidates) {
                                                                    const t = el.innerText?.trim();
                                                                    if (t && t.toLowerCase().includes('idol')) {
                                                                        el.scrollIntoView({block:'center'});
                                                                        el.click();
                                                                        return {ok: true, txt: t, partial: true};
                                                                    }
                                                                }
                                                                return {ok: false};
                                                            }
                                                        """, txt)
                                                        if _js_clicked.get("ok"):
                                                            logger.info(f"  カテゴリツリー L{level}: {txt} [JS click, partial={_js_clicked.get('partial')}]")
                                                            level_clicked = True
                                                            _human_wait(0.5, 1.0)
                                                    except Exception:
                                                        pass
                                                    if not level_clicked:
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
                                                        if level == 2:
                                                            _l2_selected_txt = txt
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

                        # (DIAG リスナーはカテゴリ選択直後に設定済み)

                        if brand_click.get("found"):
                            _human_wait(0.8, 1.2)
                            # Fix23+Fix36: EDS Select / li の両方を待つ
                            # brand_list API のレスポンスが Vue に反映されるには ~2s かかるため待機
                            try:
                                self._page.wait_for_selector(
                                    '[class*="popover"] li, [class*="dropdown"] li, [role="option"], '
                                    '[class*="eds-select__options"] [class*="option"], '
                                    '[class*="eds-option"]:not([class*="option-add"])',
                                    state="visible", timeout=4000
                                )
                                _human_wait(0.2, 0.4)  # Vue レンダリング追加バッファ
                                logger.info("  [Fix23] ドロップダウン選択肢ロード確認")
                            except Exception:
                                logger.info("  [Fix23] ドロップダウン li 待機タイムアウト（続行）")

                            # Fix36: EDS Select 対応 mouse.click アプローチ（brand license と同じ手法）
                            # class-option-regex は無関係ページ要素にマッチして誤クリックしていた
                            # → page.evaluate で visible な EDS option を探し page.mouse.click で確実に選択
                            _brand_opt_info = self._page.evaluate("""
                                () => {
                                    const isVis = (el) => {
                                        const r = el.getBoundingClientRect();
                                        return r.width > 0 && r.height > 0;
                                    };
                                    const isLeaf = (el) => {
                                        for (const c of el.children) {
                                            if (/option/i.test(c.className || '')) return false;
                                        }
                                        return true;
                                    };
                                    const allOpts = [...document.querySelectorAll(
                                        '[class*="eds-select__options"] [class*="option"], ' +
                                        '[class*="eds-option"]:not([class*="option-add"]), ' +
                                        '[class*="popover"] li, [role="option"]'
                                    )].filter(el => isVis(el) && isLeaf(el));
                                    if (allOpts.length === 0) return {found: false, reason: 'no_opts'};
                                    const texts = allOpts.slice(0, 8).map(e => e.textContent.trim());
                                    // Fix41: Camera Accessories 等のカテゴリでは NoBrand(brand_id=0) を
                                    //        選択すると「Add brand info」が ⭕ のまま → isSaveDisabled=true。
                                    //        Fix19 が POST で brand_id=0 にパッチするため、
                                    //        フロントエンド検証を通過させる最初の実ブランドを選択する。
                                    //        NoBrand(=「No brand」/「NoBrand」) は SKIP。
                                    const noBrandRx = /^(nobrand|no\\s+brand|ไม่มีแบรนด์)$/i;
                                    const realBrand = allOpts.find(el => {
                                        const t = el.textContent.trim();
                                        return !noBrandRx.test(t) &&
                                               !(t.toLowerCase().includes('no') &&
                                                 t.toLowerCase().includes('brand') && t.length < 20);
                                    });
                                    // realBrand が見つからない場合のみ NoBrand にフォールバック
                                    const noBrand = allOpts.find(el => {
                                        const t = el.textContent.trim().toLowerCase();
                                        return noBrandRx.test(t) ||
                                               (t.includes('no') && t.includes('brand') && t.length < 20);
                                    });
                                    const chosen = realBrand || noBrand || allOpts[0];
                                    const r = chosen.getBoundingClientRect();
                                    return {
                                        found: true,
                                        foundNoBrand: !realBrand && !!noBrand,
                                        choseRealBrand: !!realBrand,
                                        x: r.left + r.width / 2,
                                        y: r.top + r.height / 2,
                                        text: chosen.textContent.trim().slice(0, 60),
                                        opts: texts,
                                    };
                                }
                            """)
                            if _brand_opt_info and _brand_opt_info.get('found'):
                                self._page.mouse.click(
                                    _brand_opt_info['x'], _brand_opt_info['y'])
                                _human_wait(0.3, 0.5)
                                brand_filled = True
                                _found_nb = _brand_opt_info.get('foundNoBrand', False)
                                logger.info(
                                    f"  ブランド選択 Fix36: '{_brand_opt_info['text']}' "
                                    f"(foundNoBrand={_found_nb}, "
                                    f"opts={_brand_opt_info.get('opts')})"
                                )
                                # Vue watcher リバート対策: click 直後に native setter で値を pin
                                try:
                                    _pin = self._page.evaluate("""
                                        () => {
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
                                            const items = [...document.querySelectorAll('[class*="form-item"]')];
                                            for (const item of items) {
                                                const lbl = item.querySelector('label, [class*="label"]');
                                                if (!lbl) continue;
                                                const t = lbl.textContent.trim().replace(/[*\\s]/g,'');
                                                if (t !== 'Brand' && t !== 'แบรนด์') continue;
                                                const inp = item.querySelector('input');
                                                if (inp && inp.offsetParent !== null) {
                                                    return {ok: setNativeValue(inp, 'No Brand'), found: true};
                                                }
                                            }
                                            return {ok: false, found: false};
                                        }
                                    """)
                                    logger.info(f"  Brand native setter pin: {_pin}")
                                except Exception as _pin_e:
                                    logger.debug(f"  Brand native setter pin エラー（無視）: {_pin_e}")
                                _human_wait(0.5, 1.0)
                            else:
                                logger.info(f"  ブランド選択 Fix36: 選択肢なし ({_brand_opt_info}) → locator フォールバック")
                                # Fix36 失敗 → Playwright locator フォールバック
                                _spec_nb_regex = re.compile(r'no\s*brand|ไม่มีแบรนด์', re.IGNORECASE)
                                _spec_nb_pairs = [
                                    (self._page.locator('li').filter(has_text=_spec_nb_regex), 'li-regex'),
                                    (self._page.locator('[role="option"]').filter(has_text=_spec_nb_regex), 'role-option-regex'),
                                    (self._page.locator('[class*="popover"] li:first-child'), 'popover-first'),
                                ]
                                for opt_loc, opt_sel in _spec_nb_pairs:
                                    try:
                                        opt = opt_loc.first
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

                        # === DIAG: キャプチャしたレスポンス URL を出力 ===
                        try:
                            self._page.remove_listener('response', _diag_on_resp)
                        except Exception:
                            pass
                        if _diag_responses:
                            _seen = set()
                            for _du in _diag_responses:
                                if _du not in _seen:
                                    _seen.add(_du)
                                    logger.info(f"  [DIAG-NET] {_du[:150]}")
                        else:
                            logger.info("  [DIAG-NET] レスポンスなし（APIコールなし?）")
                        # === END DIAG ===

                        if brand_license_result.get('licenseHandled') == 'clicked':
                            _human_wait(0.8, 1.2)
                            # Fix33: page.mouse.click() でリアルなマウスイベントを発火
                            # element.click() はVueのEDS Selectに届かない場合がある
                            opt_info = self._page.evaluate("""
                                () => {
                                    const isVis = (el) => {
                                        const r = el.getBoundingClientRect();
                                        return r.width > 0 && r.height > 0;
                                    };
                                    // リーフ要素のみ取得（コンテナ除外）
                                    const isLeaf = (el) => {
                                        for (const c of el.children) {
                                            if (/option/i.test(c.className || '')) return false;
                                        }
                                        return true;
                                    };
                                    const allOpts = [...document.querySelectorAll(
                                        '[class*="eds-select__options"] [class*="option"], ' +
                                        '[class*="eds-option"]:not([class*="option-add"]), ' +
                                        '[class*="popover"] li'
                                    )].filter(el =>
                                        isVis(el)
                                        && !el.closest('[class*="form-item"]')
                                        && isLeaf(el)
                                    );
                                    if (allOpts.length === 0) return null;
                                    const texts = allOpts.slice(0, 3).map(e => e.textContent.trim());
                                    const target = allOpts.find(el =>
                                        el.textContent.trim() === 'No License Required'
                                    ) || allOpts[0];
                                    const r = target.getBoundingClientRect();
                                    return {
                                        x: r.left + r.width / 2,
                                        y: r.top + r.height / 2,
                                        text: target.textContent.trim().slice(0, 80),
                                        opts: texts
                                    };
                                }
                            """)
                            if opt_info:
                                # Fix34: page.mouse.click() でリアルクリック
                                self._page.mouse.click(opt_info['x'], opt_info['y'])
                                _human_wait(0.3, 0.5)
                                # Fix34b: 選択後 Brand License フィールドの値を確認（診断）
                                _lic_check = self._page.evaluate("""
                                    () => {
                                        const allItems = [...document.querySelectorAll('[class*="form-item"]')];
                                        for (const item of allItems) {
                                            const lbl = item.querySelector('label, [class*="label"]');
                                            if (!lbl || !lbl.textContent.includes('Brand License')) continue;
                                            const sel = item.querySelector('[class*="eds-selector"]');
                                            if (!sel) return 'no-selector';
                                            const ph = sel.querySelector('[class*="placeholder"]');
                                            const isPlaceholder = ph && ph.offsetParent !== null;
                                            return {val: sel.textContent.trim().slice(0, 60), isPlaceholder};
                                        }
                                        return 'not-found';
                                    }
                                """)
                                logger.info(f"  [Fix34-DIAG] Brand License after click: {_lic_check}")
                                # Fix34c: 選択が空 → キーボードナビで再試行
                                if isinstance(_lic_check, dict) and _lic_check.get('isPlaceholder'):
                                    logger.warning("  [Fix34c] 選択未登録 → キーボードナビ再試行")
                                    # Brand License selector に再フォーカスして ArrowDown + Enter
                                    self._page.evaluate("""
                                        () => {
                                            const allItems = [...document.querySelectorAll('[class*="form-item"]')];
                                            for (const item of allItems) {
                                                const lbl = item.querySelector('label, [class*="label"]');
                                                if (!lbl || !lbl.textContent.includes('Brand License')) continue;
                                                const sel = item.querySelector('[class*="eds-selector"]');
                                                if (sel) { sel.click(); return true; }
                                            }
                                            return false;
                                        }
                                    """)
                                    _human_wait(0.4, 0.6)
                                    self._page.keyboard.press("ArrowDown")
                                    _human_wait(0.15, 0.25)
                                    self._page.keyboard.press("Enter")
                                    _human_wait(0.3, 0.4)
                                    _lic_check2 = self._page.evaluate("""
                                        () => {
                                            const allItems = [...document.querySelectorAll('[class*="form-item"]')];
                                            for (const item of allItems) {
                                                const lbl = item.querySelector('label, [class*="label"]');
                                                if (!lbl || !lbl.textContent.includes('Brand License')) continue;
                                                const sel = item.querySelector('[class*="eds-selector"]');
                                                if (!sel) return 'no-sel';
                                                const ph = sel.querySelector('[class*="placeholder"]');
                                                return {val: sel.textContent.trim().slice(0, 60), isPlaceholder: !!(ph && ph.offsetParent !== null)};
                                            }
                                            return 'not-found';
                                        }
                                    """)
                                    logger.info(f"  [Fix34c-DIAG] Brand License after keyboard: {_lic_check2}")
                                lic_opt = opt_info['text'] + ' [opts:' + '|'.join(opt_info['opts']) + ']'
                            else:
                                lic_opt = None
                            if lic_opt:
                                logger.info(f"  Brand License選択: '{lic_opt}'")
                            else:
                                self._page.keyboard.press("Escape")
                                logger.info("  Brand License: 選択肢なし → Escape")
                                # ── Fix13: Early skip を廃止 → Pre-publish atomic に委ねる ──
                                # 101件の過去出品成功は Pre-publish atomic（No Brand + 即時Publish）で達成。
                                # Early skip では0%成功率になるため廃止。
                                # カテゴリによっては Shopee サーバーが No Brand を受け入れる。
                                _cur_brand = brand_license_result.get('brand') or ''
                                logger.warning(
                                    f"  ⚠️ Brand='{_cur_brand}' 強制（ライセンス選択肢なし）→ "
                                    f"Pre-publish atomic に委ねる（早期スキップしない）"
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
                    # ※ Brand 処理ブロックが早期例外で抜けた場合、変数が未定義のことがあるので保険
                    if not locals().get('_spec_tab_activated_for_brand', False):
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
                                // Fix10: URL/TIS/証明書/ライセンス系フィールドは空白のまま
                                // 3D印刷品はTIS認証不要のため入力してはいけない（入力すると "invalid" エラーになる）
                                // プレースホルダ: "if the product does not require TISI license, do not input this attribute"
                                if (ph.includes('http') || ph.includes('url') ||
                                    ph.includes('website') || ph.includes('link') ||
                                    ph.includes('site') || ph.includes('tis') ||
                                    lbl.includes('website') || lbl.includes('url') ||
                                    lbl.includes('tis') || lbl.includes('certificate') ||
                                    lbl.includes('certification') || lbl.includes('license')) continue;
                                // フィールドタイプをラベルとプレースホルダで判別
                                let val;
                                if (ph.includes('no.') || ph.includes('number') ||
                                           ph.includes('certificate') || ph.includes('#') ||
                                           ph.includes('code') || ph.includes('id') ||
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

            # ── Brand 再アサーション（Fix26: 廃止） ──
            # 旧実装: Spec再入力後に NoBrand を再選択していた。
            # Fix26で廃止: mandatory brand カテゴリでは NoBrand 再選択 → ライセンスクリア →
            #   Pelican戻り → disabled ループを引き起こしていた。
            # Fix19 が POST を server 送信前に intercept して brand_id=0 にパッチするため、
            # UI上のブランド表示は不要。Pelican + 偽ライセンス(Fix24) のままで
            # isSaveDisabled=false を維持するのが正しい戦略。
            logger.debug("  [Fix26] Brand再アサーション スキップ（廃止）")

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
                            # fill() は既存値をクリアしてから入力 — stale form data による蓄積バグを防ぐ
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
                            # fill() は既存値をクリアしてから入力 — stale form data による蓄積バグを防ぐ
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

                    # 重量入力 — 4戦略フォールバック (native setter + Vue proxy + keyboard)
                    # Shipping タブクリック失敗時でも独立して動作する堅牢版
                    weight_filled = self._fill_weight_robustly(weight_kg)
                    if not weight_filled:
                        logger.warning("  ⚠️ 重量入力に失敗しました（全戦略失敗）")

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

    def _fill_weight_robustly(self, weight_kg: float) -> bool:
        """Weight フィールドに kg値を入力。4戦略フォールバック。
        Shipping タブ依存を排除し、Vue v-model の revert も native setter + Vue proxy で回避する。"""
        page = self._page
        if not page or page.is_closed():
            logger.warning("  重量入力失敗: page が利用不可")
            return False

        expected = f"{weight_kg:.2f}"

        def _norm(v: str) -> str:
            try:
                return f"{float(str(v).replace(',', '').strip()):.2f}"
            except Exception:
                return str(v).strip()

        def _read(locator) -> str:
            try:
                return locator.input_value(timeout=1000).strip()
            except Exception:
                try:
                    return locator.evaluate("el => (el.value || '').trim()")
                except Exception:
                    return ""

        def _set_native(locator, value: str) -> bool:
            try:
                locator.evaluate(
                    """(el, value) => {
                        el.scrollIntoView({block: 'center', inline: 'nearest'});
                        el.focus();
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value'
                        )?.set;
                        if (setter) { setter.call(el, value); } else { el.value = value; }
                        el.dispatchEvent(new InputEvent('input', {
                            bubbles: true, composed: true, inputType: 'insertText', data: value
                        }));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                        if (typeof el.blur === 'function') el.blur();
                    }""",
                    value,
                )
                return True
            except Exception as e:
                logger.debug(f"  重量入力 native setter 失敗: {e}")
                return False

        def _verify(locator) -> bool:
            actual = _read(locator)
            ok = _norm(actual) == _norm(expected)
            logger.info(f"  重量入力検証: expected={expected}, actual={actual!r}, ok={ok}")
            return ok

        def _try_locator(locator, strategy_name: str) -> bool:
            try:
                if locator.count() == 0:
                    logger.info(f"  重量入力: {strategy_name} - 候補なし")
                    return False
                try: locator.wait_for(state="attached", timeout=2500)
                except Exception: pass
                try: locator.scroll_into_view_if_needed(timeout=1500)
                except Exception: pass
                try: locator.wait_for(state="visible", timeout=2500)
                except Exception: pass
                if not _set_native(locator, expected):
                    logger.warning(f"  重量入力: {strategy_name} - native setter 失敗")
                    return False
                _human_wait(0.2, 0.4)
                if _verify(locator):
                    logger.info(f"  重量入力完了: {expected} kg ({strategy_name})")
                    return True
                logger.warning(f"  重量入力: {strategy_name} - 値不一致")
                return False
            except Exception as e:
                logger.warning(f"  重量入力: {strategy_name} - 失敗: {e}")
                return False

        logger.info(f"  重量入力開始: {expected} kg")

        # 戦略1: 直接セレクタ
        try:
            logger.info("  重量入力: 戦略1 `.price-input:has-text(\"kg\") input`")
            direct = page.locator('.price-input:has-text("kg") input.eds-input__input').first
            if _try_locator(direct, "strategy1-direct"):
                return True
        except Exception as e:
            logger.warning(f"  重量入力: 戦略1 例外: {e}")

        # 戦略2: Weight/ kg の近傍 input を DOM からマーキング
        try:
            logger.info("  重量入力: 戦略2 `Weight` 近傍 input の探索")
            marked = page.evaluate(
                """() => {
                    document.querySelectorAll('[data-bot-weight]').forEach(e => e.removeAttribute('data-bot-weight'));
                    const inputs = [...document.querySelectorAll('input.eds-input__input, input')]
                        .filter(i => i.offsetParent !== null && !i.readOnly && !i.disabled);
                    for (const inp of inputs) {
                        let el = inp;
                        for (let i = 0; i < 5; i++) {
                            el = el.parentElement;
                            if (!el) break;
                            const text = (el.textContent || '');
                            if (text.includes('Weight') && text.includes('kg')) {
                                inp.setAttribute('data-bot-weight', 'true');
                                return true;
                            }
                        }
                    }
                    return false;
                }"""
            )
            if marked:
                marked_loc = page.locator('[data-bot-weight="true"]').first
                if _try_locator(marked_loc, "strategy2-nearby"):
                    return True
            else:
                logger.info("  重量入力: 戦略2 - マッチなし")
        except Exception as e:
            logger.warning(f"  重量入力: 戦略2 例外: {e}")

        # 戦略3: Vue 経由で modelValue / emit を触る
        try:
            logger.info("  重量入力: 戦略3 Vue 経由で値反映")
            for idx, locator in enumerate([
                page.locator('.price-input:has-text("kg") input.eds-input__input').first,
                page.locator('[data-bot-weight="true"]').first,
                page.locator('input.eds-input__input').first,
            ], start=1):
                try:
                    if locator.count() == 0:
                        continue
                    locator.wait_for(state="attached", timeout=1500)
                    result = locator.evaluate(
                        """(el, value) => {
                            const setter = Object.getOwnPropertyDescriptor(
                                HTMLInputElement.prototype, 'value'
                            )?.set;
                            if (setter) setter.call(el, value); else el.value = value;
                            el.dispatchEvent(new InputEvent('input', {
                                bubbles: true, composed: true, inputType: 'insertText', data: value
                            }));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            el.dispatchEvent(new Event('blur', {bubbles: true}));
                            if (typeof el.blur === 'function') el.blur();
                            let vm = el.__vueParentComponent || el.__vue__;
                            for (let depth = 0; vm && depth < 5; depth += 1, vm = vm.parent) {
                                try { if (typeof vm.emit === 'function') vm.emit('update:modelValue', value); } catch (_) {}
                                try { if (vm.proxy && 'modelValue' in vm.proxy) vm.proxy.modelValue = value; } catch (_) {}
                                try { if (vm.props && 'modelValue' in vm.props) vm.props.modelValue = value; } catch (_) {}
                            }
                            return (el.value || '').trim();
                        }""",
                        expected,
                    )
                    logger.info(f"  重量入力: strategy3-vue{idx} result={result!r}")
                    if _norm(result) == _norm(expected) and _verify(locator):
                        logger.info(f"  重量入力完了: {expected} kg (strategy3-vue{idx})")
                        return True
                except Exception as e:
                    logger.warning(f"  重量入力: strategy3-vue{idx} 失敗: {e}")
        except Exception as e:
            logger.warning(f"  重量入力: 戦略3 例外: {e}")

        # 戦略4: keyboard で 1 文字ずつ入力
        try:
            logger.info("  重量入力: 戦略4 keyboard type")
            fallback = None
            for locator in [
                page.locator('.price-input:has-text("kg") input.eds-input__input').first,
                page.locator('[data-bot-weight="true"]').first,
                page.locator('input.eds-input__input').first,
            ]:
                try:
                    if locator.count() and locator.is_visible():
                        fallback = locator
                        break
                except Exception:
                    continue
            if fallback is None:
                logger.warning("  重量入力: strategy4 - visible input が見つからない")
                return False
            fallback.scroll_into_view_if_needed()
            fallback.click(click_count=3)
            _human_wait(0.1, 0.2)
            page.keyboard.type(expected, delay=70)
            page.keyboard.press("Tab")
            _human_wait(0.3, 0.5)
            if _verify(fallback):
                logger.info(f"  重量入力完了: {expected} kg (strategy4-keyboard)")
                return True
            logger.warning("  重量入力: strategy4 - 値不一致")
        except Exception as e:
            logger.warning(f"  重量入力: 戦略4 失敗: {e}")

        logger.warning(f"  重量入力失敗: {expected} kg")
        return False

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
            # Fix21: NoBrand 強制を廃止。isSaveDisabled=true でボタンが永続 disabled になるため。
            # Vue の auto-select ブランド (Pelican/Fico 等) をそのまま残し、通常の Save クリックを実行。
            # Fix19 が save_product/add_product POST をインターセプトして brand_id=0 にパッチする。
            # Brand が No Brand 以外なら原子的修正（No Brand 選択 + 即時 Publish クリック）
            if False and _pre_brand and _pre_brand.lower() not in ('no brand', 'ไม่มีแบรนด์'):
                logger.warning(f"  [Pre-publish] Brand='{_pre_brand}' → 原子的 No Brand + Publish を試みる")
                # Weight 充填保証: Brand ドロップダウンを開く *前* に実行する
                # （タブ切替で dropdown が閉じてしまうため、必ず atomic 前に済ませる）
                try:
                    _wkg = getattr(self, '_last_weight_kg', 0.1)
                    logger.info(f"  [Pre-publish] Weight 事前充填: weight_kg={_wkg}")
                    try:
                        self._dismiss_chat_panel()
                    except Exception:
                        pass
                    try:
                        _ship = self._page.get_by_text("Shipping", exact=True).first
                        if _ship.count():
                            try:
                                _ship.click(timeout=5000)
                            except Exception:
                                self._page.evaluate("""
                                    () => {
                                        const tabs = [...document.querySelectorAll('[class*="tabs__nav-tab"], [class*="tab-item"]')];
                                        const t = tabs.find(el => el.textContent.trim() === 'Shipping');
                                        if (t) t.click();
                                    }
                                """)
                            _human_wait(1.0, 1.5)
                    except Exception as _sh_e:
                        logger.debug(f"  [Pre-publish] Shipping タブ切替エラー（続行）: {_sh_e}")
                    _wres = self._fill_weight_robustly(_wkg)
                    logger.info(f"  [Pre-publish] Weight 充填結果: {_wres}")
                    try:
                        _basic = self._page.get_by_text("Basic information", exact=True).first
                        if _basic.count():
                            try:
                                _basic.click(timeout=5000)
                            except Exception:
                                self._page.evaluate("""
                                    () => {
                                        const tabs = [...document.querySelectorAll('[class*="tabs__nav-tab"], [class*="tab-item"]')];
                                        const t = tabs.find(el => el.textContent.trim().startsWith('Basic'));
                                        if (t) t.click();
                                    }
                                """)
                            _human_wait(1.0, 1.5)
                    except Exception as _bi_e:
                        logger.debug(f"  [Pre-publish] Basic info タブ復帰エラー（無視）: {_bi_e}")
                except Exception as _we_e:
                    logger.debug(f"  [Pre-publish] Weight 事前充填エラー（無視）: {_we_e}")
                # Fix15: Pre-publish 直前にブランドAPIインターセプトを再確認・再インストール
                # SPA ページ上での複数商品処理でインターセプトが失われる可能性に対応
                self._reinstall_brand_intercept()
                _human_wait(0.3, 0.5)

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
                    # Fix 6: No Brand click を Playwright real click に切替
                    # 背景: JS dispatchEvent は isTrusted=false で Shopee Vue が synthetic event を
                    # 拒否し、v-model が commit されない。結果 button.disabled が解除されない。
                    # Playwright の locator.click() は OS-level mouse event (isTrusted=true) を
                    # 発火するため Vue が正しく commit する。
                    # Fix22: has-text() はcase-sensitive。ShopeeのAPIが返す brand_id=0 の
                    # display_name は "No brand"（小文字b）のため旧セレクタが不一致→誤要素クリック。
                    # re.compile で case-insensitive マッチに変更。
                    _nb_clicked_via_pw = False
                    _nb_regex = re.compile(r'no\s*brand|ไม่มีแบรนด์', re.IGNORECASE)
                    # まずドロップダウンが実際に開いているか確認してスコープ内で検索
                    _open_dd = None
                    for _dd_sel in [
                        '[class*="eds-selector__popover"]',
                        '[class*="eds-selector__dropdown"]',
                        '[class*="eds-dropdown__panel"]',
                        '[role="listbox"]',
                    ]:
                        try:
                            _dd = self._page.locator(_dd_sel).first
                            if _dd.count() and _dd.is_visible(timeout=500):
                                _open_dd = _dd
                                break
                        except Exception:
                            pass
                    _search_pairs = []
                    if _open_dd:
                        _search_pairs.append((
                            _open_dd.locator('[class*="option"], li, [role="option"]')
                            .filter(has_text=_nb_regex), 'dropdown-scoped'))
                    _search_pairs.extend([
                        (self._page.locator('[class*="option"]').filter(has_text=_nb_regex),
                         'page-option'),
                        (self._page.locator('[role="option"]').filter(has_text=_nb_regex),
                         'page-role'),
                        (self._page.locator('li').filter(has_text=_nb_regex), 'page-li'),
                    ])
                    for _nb_loc, _nb_label in _search_pairs:
                        try:
                            _nb_opt = _nb_loc.first
                            if _nb_opt.count() and _nb_opt.is_visible(timeout=2000):
                                _nb_opt.scroll_into_view_if_needed(timeout=1500)
                                _nb_opt.click(timeout=3000)
                                logger.info(f"  [Pre-publish/Fix6] No Brand Playwright click: {_nb_label}")
                                _nb_clicked_via_pw = True
                                break
                        except Exception as _nb_e:
                            logger.debug(f"  [Pre-publish/Fix6] No Brand click fail {_nb_label}: {_nb_e}")

                    # ── Fix20: Vue 3 内部状態に brand_id=0 を直接注入 ──────────────────────
                    # 根本原因: Vue の watcher が Spec 変更後に brand を名前付きブランドへ
                    # 強制復元するため isSaveDisabled が常に true → ボタンが disabled のまま。
                    # Fix16 の force-click は DOM 上の disabled 属性を除去するが、
                    # Vue の click handler 内の isSaveDisabled ガードを突破できず
                    # ネットワークリクエストが発火しない（Fix19 が一度も発火しない原因）。
                    # → Vue の setupState / data に直接 brand_id=0 を書き込み、
                    #   isSaveDisabled を false に変えてボタンを「本物 enabled」にする。
                    if _nb_clicked_via_pw:
                        try:
                            _f20 = self._page.evaluate(r"""
                                (() => {
                                    const result = {ok: false, steps: [], setCount: 0};
                                    const brandRe = /brand/i;

                                    // Step1: brand 関連 DOM 要素を探す
                                    let brandEl = null;
                                    const _candidates = [
                                        () => document.querySelector('[class*="brand"] input'),
                                        () => document.querySelector('[class*="brand"] [class*="selector"]'),
                                        () => {
                                            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                                            let node;
                                            while ((node = walker.nextNode())) {
                                                if (/^Brand\s*[*]?\s*$/.test(node.textContent.trim())) {
                                                    let p = node.parentElement;
                                                    for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
                                                        const el = p.querySelector('input, [class*="selector"]');
                                                        if (el) return el;
                                                    }
                                                }
                                            }
                                            return null;
                                        },
                                    ];
                                    for (const fn of _candidates) {
                                        try { brandEl = fn(); if (brandEl) break; } catch(e) {}
                                    }
                                    result.steps.push('brandEl:' + (brandEl
                                        ? (brandEl.tagName + '.' + (brandEl.className || '').substring(0, 40))
                                        : 'null'));

                                    // Step2: DOM要素から Vue コンポーネントを取得（祖先方向）
                                    let vueComp = null;
                                    let el = brandEl || document.body;
                                    while (el) {
                                        if (el.__vueParentComponent) { vueComp = el.__vueParentComponent; break; }
                                        el = el.parentElement;
                                    }
                                    // DOM で見つからない場合 app root から
                                    if (!vueComp) {
                                        const root = document.getElementById('app') || document.querySelector('[data-v-app]');
                                        if (root && root.__vue_app__ && root.__vue_app__._instance) {
                                            vueComp = root.__vue_app__._instance;
                                        }
                                    }
                                    if (!vueComp) { result.steps.push('no_vue_comp'); return result; }
                                    result.steps.push('vueComp:' + (vueComp.type?.name || vueComp.type?.__name || 'anon'));

                                    // Step3: brand プロパティを持つコンポーネントを祖先方向に探索
                                    // Fix22: boolean-only brand key（例: hasSelectedBrandFromRcmdBox）は
                                    // スキップして数値/オブジェクト/文字列を持つ上位コンポーネントを探す
                                    let brandComp = null;
                                    let brandCompBoolFallback = null;
                                    let curr = vueComp;
                                    for (let depth = 0; depth < 40 && curr; depth++, curr = curr.parent) {
                                        const ss = curr.setupState || {};
                                        const d  = curr.data || {};
                                        const bk = [
                                            ...Object.keys(ss).filter(k => brandRe.test(k)),
                                            ...Object.keys(d).filter(k => brandRe.test(k)),
                                        ];
                                        if (bk.length === 0) continue;
                                        const hasMeaningful = bk.some(k => {
                                            const v = (k in ss) ? ss[k] : d[k];
                                            return typeof v !== 'boolean';
                                        });
                                        if (hasMeaningful) { brandComp = curr; break; }
                                        if (!brandCompBoolFallback) brandCompBoolFallback = curr;
                                    }
                                    if (!brandComp) brandComp = brandCompBoolFallback;
                                    if (!brandComp) { result.steps.push('no_brand_comp'); return result; }

                                    const ss  = brandComp.setupState || {};
                                    const dat = brandComp.data || {};
                                    const brandKeys = [
                                        ...Object.keys(ss).filter(k => brandRe.test(k)),
                                        ...Object.keys(dat).filter(k => brandRe.test(k)),
                                    ];
                                    result.steps.push('brandComp:' + (brandComp.type?.name || brandComp.type?.__name || 'anon')
                                        + ' keys=[' + brandKeys.join(',') + ']');

                                    // Step4: brand_id=0 / NoBrand を各プロパティに書き込む
                                    for (const k of brandKeys) {
                                        try {
                                            const src = (k in ss) ? ss : dat;
                                            const v = src[k];
                                            if (typeof v === 'number') {
                                                src[k] = 0; result.setCount++;
                                            } else if (v && typeof v === 'object') {
                                                if ('brand_id' in v)  { v.brand_id = 0;  result.setCount++; }
                                                if ('brandId'  in v)  { v.brandId  = 0;  result.setCount++; }
                                                if ('name'     in v)  { v.name = 'NoBrand'; }
                                                if ('display_name' in v) { v.display_name = 'No brand'; }
                                                if ('id'       in v && /brand/i.test(k)) { v.id = 0; result.setCount++; }
                                            } else if (typeof v === 'string' && /name$/i.test(k)) {
                                                src[k] = 'NoBrand'; result.setCount++;
                                            }
                                        } catch(e) { result.steps.push('err:' + k + ':' + e.message); }
                                    }

                                    result.ok = result.setCount > 0;
                                    return result;
                                })()
                            """)
                            logger.info(f"  [Pre-publish/Fix20] Vue state injection: {_f20}")
                        except Exception as _f20e:
                            logger.warning(f"  [Pre-publish/Fix20] Vue state injection 失敗: {_f20e}")

                        # Fix20: POST リクエストを観察してボタンがネットワークを発火するか確認
                        _fix20_posts_captured = []
                        def _fix20_post_logger(req):
                            if req.method in ('POST', 'PUT') and 'shopee.co.th' in req.url:
                                body = req.post_data or ''
                                logger.info(f"[Fix20-POST] {req.method} {req.url[:120]} body={body[:120]}")
                                _fix20_posts_captured.append(req.url)
                        try:
                            self._page.on('request', _fix20_post_logger)
                        except Exception:
                            pass
                        # Vue reactivity flush を待つ
                        self._page.wait_for_timeout(600)

                    # === Publish ボタン enable 待ち（Python side ポーリング） ===
                    # Playwright click が Vue に commit を走らせるので、validation が
                    # 通れば button.disabled が外れる。最大 5 秒（200ms × 25）待機。
                    _atomic = None
                    if _nb_clicked_via_pw:
                        _pub_sel = (
                            'button:has-text("Save and Publish"), '
                            'button:has-text("Save & Publish"), '
                            'button:has-text("บันทึกและเผยแพร่"), '
                            'button:has-text("เผยแพร่")'
                        )
                        # Fix13: 高速ポーリング（50ms × 60 = 3s）で Vue ブランド復元前の
                        # 瞬間的 enabled 状態を捕捉する。また enabled 未検出時は force click を試みる。
                        _btn_enabled = False
                        _last_disabled_by = 'init'
                        for _poll_i in range(120):  # 50ms × 120 = 6 seconds（Fix20 Vue 反映待ち延長）
                            try:
                                _diag = self._page.evaluate(f"""
                                    () => {{
                                        const btns = [...document.querySelectorAll('button')]
                                            .filter(b => b.offsetParent !== null);
                                        let btn = null;
                                        for (const b of btns) {{
                                            const t = b.textContent.trim();
                                            if (t.includes('Save and Publish') ||
                                                t.includes('Save & Publish') ||
                                                t.includes('บันทึกและเผยแพร่') ||
                                                t.includes('เผยแพร่')) {{
                                                btn = b; break;
                                            }}
                                        }}
                                        if (!btn) return 'no_button';
                                        const reasons = [];
                                        if (btn.disabled) reasons.push('disabled-prop');
                                        if (btn.hasAttribute('disabled')) reasons.push('disabled-attr');
                                        if (btn.className.includes('disabled')) reasons.push('disabled-class');
                                        if (btn.getAttribute('aria-disabled') === 'true') reasons.push('aria-disabled');
                                        return reasons.length > 0 ? reasons.join(',') : 'enabled';
                                    }}
                                """)
                                _last_disabled_by = _diag
                                if _diag == 'enabled':
                                    _btn_enabled = True
                                    break
                            except Exception:
                                pass
                            self._page.wait_for_timeout(50)
                        if _btn_enabled:
                            # Playwright で Publish を real click
                            try:
                                self._page.locator(_pub_sel).first.click(timeout=3000)
                                _atomic = {'noBrand': True, 'publish': True, 'via': 'Fix6-pw-real-click'}
                            except Exception as _pc_e:
                                logger.warning(f"  [Pre-publish/Fix6] Publish click 失敗: {_pc_e}")
                                _atomic = {'noBrand': True, 'publish': False, 'reason': 'pw_click_fail'}
                        else:
                            # Fix16: button が disabled のまま → JS で disabled 除去 → Playwright force=True click
                            # isTrusted=false の JS click と違い、Playwright force click は OS-level event で isTrusted=true
                            logger.warning(f"  [Pre-publish/Fix16] button_disabled({_last_disabled_by}) → disabled除去 + Playwright force click")
                            try:
                                # Step A: JS で disabled 属性/プロパティを削除
                                _rm_ok = self._page.evaluate("""
                                    () => {
                                        const btns = [...document.querySelectorAll('button')].filter(b => b.offsetParent !== null);
                                        for (const b of btns) {
                                            const t = b.textContent.trim();
                                            if (t.includes('Save and Publish') || t.includes('บันทึกและเผยแพร่') || t.includes('เผยแพร่')) {
                                                b.removeAttribute('disabled');
                                                b.disabled = false;
                                                // data 属性でマーキング（Playwright locator用）
                                                b.setAttribute('data-fix16-pub', '1');
                                                return true;
                                            }
                                        }
                                        return false;
                                    }
                                """)
                                if _rm_ok:
                                    # Step B: Playwright real click（force=True で disabled 再チェック回避）
                                    _pub_btn = self._page.locator('[data-fix16-pub="1"]').first
                                    _pub_btn.click(force=True, timeout=3000)
                                    _atomic = {'noBrand': True, 'publish': True, 'via': 'Fix16-force-pw-click'}
                                    logger.info("  [Pre-publish/Fix16] disabled除去 + Playwright force click 送信")
                                else:
                                    _atomic = {'noBrand': True, 'publish': False, 'reason': 'button_not_found'}
                            except Exception as _fc_e:
                                logger.warning(f"  [Pre-publish/Fix16] force click 失敗: {_fc_e}")
                                _atomic = {'noBrand': True, 'publish': False, 'reason': 'button_disabled',
                                           'disabled_by': _last_disabled_by}
                            # Fix 7: button_disabled 時、どのフィールドが validation 失敗か全dump
                            try:
                                _fdiag = self._page.evaluate("""
                                    () => {
                                        const findInputNear = (labelText) => {
                                            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                                            let node;
                                            while ((node = walker.nextNode())) {
                                                if (node.textContent.trim().replace(/[*\\s]/g,'') !== labelText) continue;
                                                const par = node.parentElement;
                                                if (!par || par.offsetParent === null) continue;
                                                let c = par;
                                                for (let i = 0; i < 8; i++) {
                                                    if (!c) break;
                                                    const inp = c.querySelector('input, textarea');
                                                    if (inp) return inp.value;
                                                    const sel = c.querySelector('[class*="eds-selector"]');
                                                    if (sel && sel.textContent.trim().length < 60) return sel.textContent.trim();
                                                    c = c.parentElement;
                                                }
                                            }
                                            return null;
                                        };
                                        return {
                                            brand: findInputNear('Brand'),
                                            weight: findInputNear('Weight'),
                                            category: findInputNear('Category'),
                                            errFields: [...document.querySelectorAll('[class*="is-error"], [class*="has-error"]')]
                                                .filter(el => el.offsetParent !== null)
                                                .slice(0, 20)
                                                .map(el => (el.textContent || '').slice(0, 120).replace(/\\s+/g,' ').trim()),
                                            errMsgs: [...document.querySelectorAll(
                                                '[class*="form-item__error"], [class*="error-tip"], ' +
                                                '[class*="error-msg"], [class*="eds-form-item__error"]'
                                            )]
                                                .filter(el => el.offsetParent !== null)
                                                .slice(0, 20)
                                                .map(el => (el.textContent || '').trim())
                                                .filter(t => t),
                                            requiredEmptyStars: [...document.querySelectorAll('[class*="required"], [class*="is-required"]')]
                                                .filter(el => el.offsetParent !== null)
                                                .slice(0, 15)
                                                .map(el => (el.textContent || '').slice(0, 80).replace(/\\s+/g,' ').trim())
                                        };
                                    }
                                """)
                                logger.warning(f"  [Pre-publish/Fix7] 🔬 field diagnostic: {_fdiag}")
                            except Exception as _fd_e:
                                logger.debug(f"  [Pre-publish/Fix7] diagnostic fail: {_fd_e}")
                    else:
                        _atomic = {'noBrand': False, 'publish': False, 'reason': 'no_option'}
                    # Fix20: POST キャプチャ結果を記録
                    try:
                        if _fix20_posts_captured:
                            logger.info(f"  [Fix20-POST-SUMMARY] {len(_fix20_posts_captured)} POST(s) captured: {_fix20_posts_captured[:5]}")
                        else:
                            logger.warning("  [Fix20-POST-SUMMARY] ネットワークリクエスト 0 件 → Vue click handler がブロック中（isSaveDisabled=true）")
                    except NameError:
                        pass
                    logger.info(f"  [Pre-publish] 原子的操作結果: {_atomic}")
                    if _atomic and _atomic.get('publish'):
                        logger.info("  [Pre-publish] No Brand + Save and Publish 原子的実行完了 ✅")
                        clicked = True
                    elif _atomic and _atomic.get('noBrand') and not _atomic.get('publish'):
                        logger.warning("  [Pre-publish] No Brand 選択済みだが Publish ボタン未発見 → 通常フローで続行")
                    else:
                        # No Brand option も見つからなかった → 従来のフォールバック
                        logger.warning("  [Pre-publish] No Brand option 見当たらず → native setter フォールバック")
                        try:
                            self._page.keyboard.press("Escape")
                        except Exception:
                            pass
                        _human_wait(0.5, 0.8)
                        _pw_nb_done = False
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
            # Fix43: 出品ボタンクリック前診断 — isSaveDisabled の実体を特定する
            try:
                _pre_diag = self._page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button')];
                        const targetBtn = btns.find(b =>
                            b.textContent.trim().includes('Save and Publish') ||
                            b.textContent.trim().includes('Save & Publish') ||
                            b.textContent.trim().includes('เผยแพร่')
                        );
                        if (!targetBtn) return {found: false};

                        const diag = {
                            found: true,
                            btnDisabled: targetBtn.disabled,
                            btnClass: (targetBtn.className || '').slice(0, 100),
                            veiKeys: [],
                            ancestorVeiKeys: [],
                            compMethods: [],
                            boolCtxKeys: [],
                        };

                        // Fix43a: _vei を button から祖先まで探す
                        let el = targetBtn;
                        for (let i = 0; i < 20 && el; i++, el = el.parentElement) {
                            if (el._vei) {
                                const keys = Object.keys(el._vei);
                                diag.ancestorVeiKeys.push({depth: i, tag: el.tagName, keys});
                                if (i === 0) diag.veiKeys = keys;
                                break; // 最初に見つかった祖先
                            }
                        }

                        // Fix43b: Vue コンポーネントツリーを button から上へ走査し
                        //         submit/save/publish 系メソッドと boolean ctx getter を探す
                        let vueEl = targetBtn;
                        for (let j = 0; j < 30 && vueEl; j++, vueEl = vueEl.parentElement) {
                            const inst = vueEl.__vueParentComponent;
                            if (!inst) continue;
                            const ctx = inst.proxy || inst.ctx;
                            if (!ctx) continue;

                            // Method 探索
                            try {
                                const keys = Object.keys(ctx).filter(k => {
                                    try {
                                        const v = ctx[k];
                                        return typeof v === 'function' &&
                                               /save|publish|submit|Send|confirm/i.test(k);
                                    } catch(_) { return false; }
                                });
                                if (keys.length > 0) diag.compMethods.push({depth: j, tag: vueEl.tagName, keys});
                            } catch(_) {}

                            // Boolean ctx getter 探索 (disabled/save/publish 系のみ)
                            try {
                                const descs = Object.getOwnPropertyDescriptors(ctx);
                                const boolKeys = Object.keys(descs).filter(k => {
                                    if (!/disab|save|publish|valid|submit|isSave/i.test(k)) return false;
                                    try {
                                        const v = descs[k].get ? descs[k].get.call(ctx) : ctx[k];
                                        return typeof v === 'boolean';
                                    } catch(_) { return false; }
                                }).map(k => {
                                    try {
                                        const v = descs[k].get ? descs[k].get.call(ctx) : ctx[k];
                                        return `${k}=${v}`;
                                    } catch(_) { return `${k}=err`; }
                                });
                                if (boolKeys.length > 0) diag.boolCtxKeys.push({depth: j, keys: boolKeys});
                            } catch(_) {}
                        }
                        return diag;
                    }
                """)
                logger.info(f"  [Fix43-DIAG] pre-click: {_pre_diag}")
            except Exception as _diag_e:
                logger.debug(f"  [Fix43-DIAG] エラー（無視）: {_diag_e}")

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
                        except Exception as _btn_exc:
                            logger.info(f"  通常クリック例外: {type(_btn_exc).__name__}: {str(_btn_exc)[:120]}")
                            # Fix27: チャットウィジェットのオーバーレイ + Vue isSaveDisabled 両方を回避。
                            # Fix25 v4 は 'isSaveDisabled' をキー名で探したが本番バンドルで難読化済。
                            # Fix27: setupState の全 ComputedRefImpl (boolean=true) を shotgun で false にパッチし
                            # element.click() → Vue @click ハンドラの guard を通過させて POST を発火させる。
                            # Fix30: __vueParentComponent は Shopee 本番バンドルで DOM 要素から削除済み。
                            # 方法1(DOM walk)が空振りした場合の方法2: __vue_app__._instance から
                            # BFS でコンポーネントツリーを降り、全 ComputedRef(bool=true) をパッチ。
                            logger.info("  通常クリック失敗 → Fix30: vue_app BFS + ctx.getter patch (Fix31) + element.click()")
                            _js_clicked = self._page.evaluate("""
                                () => {
                                    // ── Fix30: DOM walk + App root BFS で Vue ComputedRef を全パッチ ──
                                    const btns = [...document.querySelectorAll('button')];
                                    let targetBtn = null;
                                    for (const b of btns) {
                                        const txt = b.textContent.trim();
                                        if (
                                            txt.includes('Save and Publish') ||
                                            txt.includes('Save & Publish') ||
                                            txt.includes('บันทึกและเผยแพร่') ||
                                            txt.includes('เผยแพร่') ||
                                            (txt.includes('Publish') && txt.length < 25)
                                        ) { targetBtn = b; break; }
                                    }
                                    if (!targetBtn) return {clicked: false, reason: 'no button'};

                                    const patchedKeys = [];
                                    let compCount = 0;

                                    // Fix31: Vue3 Options API の computed は inst.ctx に getter として保存される
                                    // Fix38: Vue3 Composition API (<script setup>) の computed は
                                    //        inst.setupState に ComputedRef として保存される。
                                    //        getOwnPropertyDescriptors(ctx) では見えないため
                                    //        setupState を直接スキャンして __v_isRef:true な
                                    //        boolean=true の ComputedRef を全て false にパッチする。
                                    //        また MAX_COMP を 300→2000 に引き上げ深いコンポーネントも捕捉。
                                    let firstInstKeys = null;
                                    let _fix49Comp = null;       // Fix49: disabledConfirm コンポーネント
                                    let _fix49AllMethods = [];   // Fix49: そのコンポーネントの全メソッド名
                                    function patchComp(inst) {
                                        if (!firstInstKeys) {
                                            try { firstInstKeys = Object.keys(inst).slice(0, 30); } catch(_) {}
                                        }
                                        // ── Fix45: inst.props.disabled をパッチ ──
                                        // EDS Button は props.disabled を内部で参照して click を早期リターンする。
                                        // DOM の disabled 属性や prototype override では防げないため
                                        // Vue コンポーネントの props.disabled を直接 false に書き換える。
                                        try {
                                            if (inst.props && inst.props.disabled === true) {
                                                Object.defineProperty(inst.props, 'disabled', {
                                                    get: () => false, configurable: true
                                                });
                                                patchedKeys.push('props.disabled');
                                            }
                                        } catch(_) {}
                                        // ── 方法A: inst.ctx の getter ディスクリプタをパッチ (Options API) ──
                                        const ctxObj = inst.ctx;
                                        if (ctxObj && typeof ctxObj === 'object') {
                                            let descs;
                                            try { descs = Object.getOwnPropertyDescriptors(ctxObj); } catch(_) {}
                                            if (descs) {
                                                for (const key of Object.keys(descs)) {
                                                    const desc = descs[key];
                                                    if (typeof desc.get === 'function') {
                                                        try {
                                                            const cv = desc.get.call(ctxObj);
                                                            if (typeof cv === 'boolean' && cv === true) {
                                                                Object.defineProperty(ctxObj, key, {
                                                                    get: () => false, configurable: true
                                                                });
                                                                patchedKeys.push(`ctx.get:${key}`);
                                                                // Fix49: disabledConfirm コンポーネントを記録
                                                                if (key === 'disabledConfirm' && !_fix49Comp) _fix49Comp = inst;
                                                            }
                                                        } catch(_) {}
                                                    }
                                                }
                                            }
                                        }
                                        // ── 方法B: inst.proxy の getter ディスクリプタ (フォールバック) ──
                                        const proxyObj = inst.proxy;
                                        if (proxyObj && proxyObj !== ctxObj && typeof proxyObj === 'object') {
                                            let descs2;
                                            try { descs2 = Object.getOwnPropertyDescriptors(proxyObj); } catch(_) {}
                                            if (descs2) {
                                                for (const key of Object.keys(descs2)) {
                                                    const desc = descs2[key];
                                                    if (typeof desc.get === 'function') {
                                                        try {
                                                            const cv = desc.get.call(proxyObj);
                                                            if (typeof cv === 'boolean' && cv === true) {
                                                                Object.defineProperty(proxyObj, key, {
                                                                    get: () => false, configurable: true
                                                                });
                                                                patchedKeys.push(`proxy.get:${key}`);
                                                            }
                                                        } catch(_) {}
                                                    }
                                                }
                                            }
                                        }
                                        // ── 方法C: Fix38 - inst.setupState の ComputedRef をパッチ (Composition API) ──
                                        // Vue 3 <script setup> では computed は setupState に
                                        // ComputedRefImpl { __v_isRef:true, _value:boolean } として保存される。
                                        // getOwnPropertyDescriptors(ctx) では捕捉不可なので直接スキャン。
                                        const ss = inst.setupState;
                                        if (ss && typeof ss === 'object') {
                                            // Vue 3 reactive/readonly proxy の raw object を取得
                                            let rawSs = ss;
                                            try { if (ss.__v_raw) rawSs = ss.__v_raw; } catch(_) {}
                                            try {
                                                for (const key of Object.keys(rawSs)) {
                                                    try {
                                                        const val = rawSs[key];
                                                        // ComputedRef の条件: __v_isRef=true かつ _value が boolean true
                                                        if (val && val.__v_isRef === true &&
                                                            typeof val._value === 'boolean' && val._value === true) {
                                                            // _value を直接 false に書き換え（キャッシュ上書き）
                                                            val._value = false;
                                                            // .value ゲッターもオーバーライド
                                                            // （computed 再評価で true に戻らないように）
                                                            try {
                                                                Object.defineProperty(val, 'value', {
                                                                    get: () => false, configurable: true
                                                                });
                                                            } catch(_) {}
                                                            patchedKeys.push(`setup:ref:${key}`);
                                                        }
                                                    } catch(_) {}
                                                }
                                            } catch(_) {}
                                        }
                                    }

                                    // ── 方法1: DOM要素の __vueParentComponent を上向きに巡回 ──
                                    let el = targetBtn;
                                    const visited1 = new Set();
                                    while (el && el !== document.body) {
                                        const comp = el.__vueParentComponent;
                                        if (comp && !visited1.has(comp)) {
                                            visited1.add(comp);
                                            patchComp(comp);
                                        }
                                        el = el.parentElement;
                                    }

                                    // ── 方法2: __vue_app__._instance から BFS でコンポーネントツリーを降る ──
                                    // (本番バンドルで __vueParentComponent が DOM から削除されている場合のフォールバック)
                                    const rootEl = document.querySelector('[data-v-app]') ||
                                                   document.getElementById('app') ||
                                                   document.querySelector('#app');
                                    const vueApp = rootEl && rootEl.__vue_app__;
                                    const diagApp = {
                                        appFound: !!vueApp,
                                        instanceFound: !!(vueApp && vueApp._instance),
                                        patchBefore: patchedKeys.length
                                    };

                                    if (vueApp && vueApp._instance) {
                                        const MAX_COMP = 2000; // Fix38: 300→2000
                                        const appVisited = new Set();
                                        const queue = [vueApp._instance];

                                        function walkVNode(vnode) {
                                            if (!vnode || typeof vnode !== 'object') return;
                                            if (vnode.component && !appVisited.has(vnode.component)) {
                                                queue.push(vnode.component);
                                            }
                                            if (Array.isArray(vnode.children)) {
                                                for (const c of vnode.children) walkVNode(c);
                                            } else if (vnode.children && typeof vnode.children === 'object' &&
                                                       !Array.isArray(vnode.children)) {
                                                // slot オブジェクト: { default: fn }
                                                for (const slotFn of Object.values(vnode.children)) {
                                                    if (typeof slotFn === 'function') {
                                                        try {
                                                            const slotVnodes = slotFn();
                                                            if (Array.isArray(slotVnodes)) {
                                                                for (const sv of slotVnodes) walkVNode(sv);
                                                            }
                                                        } catch(_) {}
                                                    }
                                                }
                                            }
                                        }

                                        while (queue.length > 0 && compCount < MAX_COMP) {
                                            const comp = queue.shift();
                                            if (!comp || appVisited.has(comp)) continue;
                                            appVisited.add(comp);
                                            compCount++;
                                            patchComp(comp);
                                            if (comp.subTree) walkVNode(comp.subTree);
                                        }
                                        diagApp.compWalked = compCount;
                                        diagApp.patchAfter = patchedKeys.length;
                                        diagApp.firstInstKeys = firstInstKeys;
                                        // Fix49: disabledConfirm コンポーネントのメソッドを列挙（診断用）
                                        if (_fix49Comp) {
                                            for (const _o49 of [_fix49Comp.ctx, _fix49Comp.proxy, _fix49Comp.setupState].filter(Boolean)) {
                                                try {
                                                    const _ks49 = [...new Set([...Object.keys(_o49), ...Object.getOwnPropertyNames(_o49)])];
                                                    for (const _k49 of _ks49) {
                                                        try {
                                                            if (typeof _o49[_k49] === 'function' && !_fix49AllMethods.includes(_k49)) {
                                                                _fix49AllMethods.push(_k49);
                                                            }
                                                        } catch(_) {}
                                                    }
                                                } catch(_) {}
                                            }
                                        }
                                        diagApp.fix49Comp = !!_fix49Comp;
                                        diagApp.fix49Methods = _fix49AllMethods.slice(0, 60);
                                    }

                                    // Fix39: fetch spy / Fix40: pointer-events 除去 + 座標返却
                                    // element.click() は isTrusted=false → ハンドラ早期リターン確定。
                                    // Fix40: pointer-events:none を除去して Python 側が page.mouse.click()
                                    //        (isTrusted=true) を発火できるよう座標を返す。
                                    const wasDisabled = targetBtn.disabled;
                                    const setupRefs = patchedKeys.filter(k => k.startsWith('setup:'));

                                    // Fix40: pointer-events 制約除去 + 座標取得
                                    targetBtn.style.pointerEvents = 'auto';
                                    targetBtn.style.zIndex = '99999';
                                    let _pe40 = targetBtn.parentElement;
                                    for (let _pi = 0; _pi < 6 && _pe40; _pi++) {
                                        _pe40.style.pointerEvents = 'auto';
                                        _pe40 = _pe40.parentElement;
                                    }
                                    // チャット/サポートウィジェットを pointer-events 無効化
                                    ['[class*="chat-widget"]','[class*="ChatWidget"]',
                                     '[class*="support-widget"]','[id*="chat-widget"]',
                                     '[class*="live-chat"]','[class*="helpdesk"]',
                                     '.shopee-chat','[class*="bubble"]'].forEach(sel => {
                                        try { document.querySelectorAll(sel).forEach(
                                            el => { el.style.pointerEvents = 'none'; });
                                        } catch(_) {}
                                    });
                                    const _btnR40 = targetBtn.getBoundingClientRect();
                                    const _btnX40 = _btnR40.left + _btnR40.width / 2;
                                    const _btnY40 = _btnR40.top + _btnR40.height / 2;
                                    targetBtn.focus();

                                    // Fix39: fetch spy (element.click 後の診断用)
                                    const _capturedFetches = [];
                                    const _origFetch = window.fetch;
                                    window.fetch = function(url, opts) {
                                        _capturedFetches.push({
                                            url: String(url).slice(0, 120),
                                            method: (opts && opts.method) || 'GET',
                                            bodySnippet: (opts && typeof opts.body === 'string')
                                                ? opts.body.slice(0, 200) : '?'
                                        });
                                        return _origFetch.apply(this, arguments);
                                    };
                                    const _capturedXhr = [];
                                    const _origOpen = XMLHttpRequest.prototype.open;
                                    XMLHttpRequest.prototype.open = function(method, url) {
                                        _capturedXhr.push({ method, url: String(url).slice(0, 120) });
                                        return _origOpen.apply(this, arguments);
                                    };

                                    // Fix51: store.getters をグローバルプロキシして handleSave のガードを特定
                                    // この proxy は以降の全クリック（dialog click, Fix49 click）でも有効なので
                                    // getterAccess51 が埋まった段階でどのゲッターが true を返しているかわかる。
                                    const _getterAccess51 = {};
                                    const _dispatchLog51 = [];
                                    const _getterPatched51 = [];
                                    try {
                                        const _app51 = document.querySelector('[data-v-app]').__vue_app__;
                                        const _proxy51 = _app51._instance && _app51._instance.proxy;
                                        const _store51 = _proxy51 && _proxy51.$store;
                                        if (_store51) {
                                            // store.getters proxy
                                            try {
                                                const _origGetters51 = _store51.getters;
                                                const _pxGetters51 = new Proxy(_origGetters51, {
                                                    get(target, prop) {
                                                        const val = Reflect.get(target, prop);
                                                        if (typeof prop === 'string' &&
                                                            prop !== '__v_isReadonly' && prop !== '__v_isRef' &&
                                                            prop !== '__v_isReactive' && prop !== '__v_raw' &&
                                                            prop !== 'constructor' && prop !== 'then') {
                                                            _getterAccess51[prop] = val;
                                                            // boolean true → patch to false
                                                            if (typeof val === 'boolean' && val === true) {
                                                                try {
                                                                    Object.defineProperty(target, prop, { get: () => false, configurable: true });
                                                                    if (!_getterPatched51.includes(prop)) _getterPatched51.push(prop);
                                                                } catch(_) {}
                                                                return false;
                                                            }
                                                        }
                                                        return val;
                                                    },
                                                    has(target, prop) { return Reflect.has(target, prop); },
                                                    ownKeys(target) { return Reflect.ownKeys(target); }
                                                });
                                                Object.defineProperty(_store51, 'getters', {
                                                    get: () => _pxGetters51, configurable: true
                                                });
                                            } catch(_) {}
                                            // store.dispatch proxy
                                            try {
                                                const _origDisp51 = _store51.dispatch.bind(_store51);
                                                _store51.dispatch = function(type, payload) {
                                                    _dispatchLog51.push(String(type));
                                                    return _origDisp51(type, payload);
                                                };
                                            } catch(_) {}
                                        }
                                    } catch(_) {}

                                    return new Promise(resolve => {
                                        Promise.resolve().then(() => {
                                            Promise.resolve().then(async () => {
                                                // Fix44: HTMLButtonElement.prototype.disabled を
                                                //        グローバルオーバーライド（Vue 再レンダー完全無効化）
                                                // Vue が disabled=true を書き戻しても
                                                // ブラウザの click ブロックが発生しないよう prototype 全体を書き換える。
                                                let _btnProtoPatched = false;
                                                try {
                                                    Object.defineProperty(HTMLButtonElement.prototype, 'disabled', {
                                                        get: () => false,
                                                        set: () => { /* no-op — prevent Vue from re-disabling */ },
                                                        configurable: true
                                                    });
                                                    _btnProtoPatched = true;
                                                } catch(_) {}

                                                // setAttribute('disabled') もブロック
                                                let _setAttrPatched = false;
                                                try {
                                                    const _origSetAttr = Element.prototype.setAttribute;
                                                    Element.prototype.setAttribute = function(name, value) {
                                                        if (this.tagName === 'BUTTON' && name === 'disabled') return;
                                                        return _origSetAttr.call(this, name, value);
                                                    };
                                                    _setAttrPatched = true;
                                                } catch(_) {}

                                                // CSS で eds-button--disabled の pointer-events を解除
                                                try {
                                                    const _style44 = document.createElement('style');
                                                    _style44.id = 'fix44-override';
                                                    _style44.textContent =
                                                        '.eds-button--disabled, button[disabled], button.is-disabled' +
                                                        '{ pointer-events: auto !important; opacity: 1 !important; }';
                                                    document.head.appendChild(_style44);
                                                } catch(_) {}

                                                // Fix42: disabled を instance-level でもロック
                                                try {
                                                    Object.defineProperty(targetBtn, 'disabled', {
                                                        get: () => false,
                                                        set: (v) => {},
                                                        configurable: true
                                                    });
                                                } catch(_) {}
                                                // Fix46: HTML content attribute を必ず除去する
                                                // (JS prototype getter の override だけでは
                                                //  ブラウザの click イベント dispatch が抑制される)
                                                targetBtn.removeAttribute('disabled');
                                                targetBtn.classList.remove('is-disabled');
                                                targetBtn.classList.remove('eds-button--disabled');
                                                targetBtn.removeAttribute('aria-disabled');
                                                targetBtn.style.pointerEvents = 'auto';

                                                // Fix42+43: Vue _vei handler を直接呼び出す
                                                // _vei は button 自身にない場合、祖先 DOM 要素を登る。
                                                let _veiCalled = false;
                                                let _veiFound = false;
                                                let _veiDepth = -1;
                                                try {
                                                    let _veiEl = targetBtn;
                                                    for (let _vi = 0; _vi < 15 && _veiEl; _vi++, _veiEl = _veiEl.parentElement) {
                                                        const _vei = _veiEl._vei;
                                                        if (!_vei) continue;
                                                        const _onClick = _vei.onClick || _vei['onClick'];
                                                        const _handler = _onClick && (_onClick.value || _onClick);
                                                        if (typeof _handler === 'function') {
                                                            _veiFound = true;
                                                            _veiDepth = _vi;
                                                            const _ev = new MouseEvent('click', {
                                                                bubbles: true, cancelable: true, composed: true,
                                                                view: window
                                                            });
                                                            _handler(_ev);
                                                            _veiCalled = true;
                                                            break;
                                                        }
                                                    }
                                                } catch(_e) { }

                                                // Fix43c: _vei が見つからない場合、Vue コンポーネントのメソッドを直接呼ぶ
                                                let _compCalled = false;
                                                let _compMethod = '';
                                                if (!_veiCalled) {
                                                    try {
                                                        let _cEl = targetBtn;
                                                        for (let _ci = 0; _ci < 30 && _cEl; _ci++, _cEl = _cEl.parentElement) {
                                                            const _inst = _cEl.__vueParentComponent;
                                                            if (!_inst) continue;
                                                            const _ctx = _inst.proxy || _inst.ctx;
                                                            if (!_ctx) continue;
                                                            const _keys = Object.keys(_ctx).filter(k => {
                                                                try {
                                                                    return typeof _ctx[k] === 'function' &&
                                                                           /^(handleSave|handlePublish|onSave|onPublish|submit|handleSubmit|handleConfirm|onClickSave|clickSave|doSave|doPublish)$/.test(k);
                                                                } catch(_) { return false; }
                                                            });
                                                            if (_keys.length > 0) {
                                                                _ctx[_keys[0]]();
                                                                _compCalled = true;
                                                                _compMethod = _keys[0];
                                                                break;
                                                            }
                                                        }
                                                    } catch(_e) {}
                                                }

                                                // Fallback: _vei が無ければ element.click()
                                                // Fix46: removeAttribute('disabled') 後なので
                                                //        click() が実際にイベントを発火するはず
                                                if (!_veiCalled) {
                                                    try { targetBtn.click(); } catch(_) {}
                                                }

                                                // Fix49: disabledConfirm コンポーネントのメソッドを直接呼ぶ
                                                let _fix49Called = false;
                                                let _fix49CallMethod = '';
                                                if (_fix49Comp && _fix49AllMethods.length > 0) {
                                                    // save/submit/publish/confirm 系のメソッドを呼ぶ
                                                    const _re49 = /^(save|submit|publish|handleSave|handleSubmit|handlePublish|handleConfirm|onSave|onSubmit|onPublish|clickSave|doSave|doPublish|confirmSave)/i;
                                                    for (const _o49c of [_fix49Comp.ctx, _fix49Comp.proxy].filter(Boolean)) {
                                                        if (_fix49Called) break;
                                                        for (const _k49c of _fix49AllMethods) {
                                                            if (_fix49Called) break;
                                                            if (_re49.test(_k49c)) {
                                                                try { _o49c[_k49c](); _fix49Called = true; _fix49CallMethod = _k49c; } catch(_) {}
                                                            }
                                                        }
                                                    }
                                                }

                                                // Fix49b: Vuex _actions を Reflect.ownKeys で列挙してディスパッチ
                                                let _fix49VxActions = [];
                                                let _fix49VxStateKeys = [];
                                                let _fix49VxDispatch = '';
                                                try {
                                                    const _app49b = document.querySelector('[data-v-app]').__vue_app__;
                                                    const _proxy49b = _app49b._instance && _app49b._instance.proxy;
                                                    if (_proxy49b && _proxy49b.$store) {
                                                        const _s49 = _proxy49b.$store;
                                                        try { _fix49VxActions = Reflect.ownKeys(_s49._actions || {}).filter(k => typeof k === 'string').slice(0, 40); } catch(_) {}
                                                        try {
                                                            const _raw49 = _s49._state && _s49._state.data;
                                                            if (_raw49) _fix49VxStateKeys = Reflect.ownKeys(_raw49).filter(k => typeof k === 'string').slice(0, 20);
                                                        } catch(_) {}
                                                        // save/submit 系アクションをディスパッチ
                                                        const _actTry = [
                                                            ..._fix49VxActions.filter(k => /save|submit|publish/i.test(k)).slice(0, 5),
                                                            'listing/submit', 'listing/saveAndPublish', 'listing/save',
                                                            'product/submit', 'product/save', 'addProduct/save',
                                                        ];
                                                        for (const _act of _actTry) {
                                                            if (_fix49VxDispatch) break;
                                                            try { await _s49.dispatch(_act); _fix49VxDispatch = _act; } catch(_) {}
                                                        }
                                                    }
                                                } catch(_) {}

                                                // Fix46: Pinia store 経由で submit を直接呼ぶ（フォールバック）
                                                let _pinaStoreKeys = [];
                                                let _pinaSubmitCalled = false;
                                                let _pinaSubmitMethod = '';
                                                let _pinaDisabledKeys = [];
                                                try {
                                                    // Pinia は Symbol('pinia') キーで provides に入る
                                                    const _app46 = document.querySelector('[data-v-app]').__vue_app__;
                                                    const _provides46 = _app46._instance.provides;
                                                    const _pinaSymbol = Object.getOwnPropertySymbols(_provides46)
                                                        .find(s => String(s).includes('pinia'));
                                                    if (_pinaSymbol) {
                                                        const _pinia = _provides46[_pinaSymbol];
                                                        _pinaStoreKeys = Object.keys(_pinia.state.value || {});
                                                        // product/listing/add 系ストアを探す
                                                        const _prodKey = _pinaStoreKeys.find(k =>
                                                            /product|listing|add|publish/i.test(k));
                                                        if (_prodKey) {
                                                            // store にアクセス
                                                            const _stores = _pinia._s;  // Map<id, store>
                                                            if (_stores) {
                                                                _stores.forEach((store, id) => {
                                                                    // isSaveDisabled 関連キーを調査
                                                                    try {
                                                                        const _disKeys = Object.keys(store).filter(k =>
                                                                            /save|disabled|publish|submit/i.test(k)
                                                                        );
                                                                        if (_disKeys.length > 0) {
                                                                            _pinaDisabledKeys.push({id, keys: _disKeys.slice(0, 5)});
                                                                        }
                                                                        // submit 系アクションを直接呼ぶ
                                                                        const _submitKeys = Object.keys(store).filter(k =>
                                                                            typeof store[k] === 'function' &&
                                                                            /^(submit|onSubmit|handleSubmit|saveProduct|publishProduct|handleSave|handlePublish|onSave|onPublish)$/.test(k)
                                                                        );
                                                                        if (_submitKeys.length > 0 && !_pinaSubmitCalled) {
                                                                            store[_submitKeys[0]]();
                                                                            _pinaSubmitCalled = true;
                                                                            _pinaSubmitMethod = `${id}.${_submitKeys[0]}`;
                                                                        }
                                                                    } catch(_) {}
                                                                });
                                                            }
                                                        }
                                                    }
                                                } catch(_) {}

                                                // Fix42: 非同期 fetch を待つ（Vue handler は async）
                                                await new Promise(r => setTimeout(r, 2000));
                                                window.fetch = _origFetch;
                                                XMLHttpRequest.prototype.open = _origOpen;
                                                resolve({
                                                    clicked: true,
                                                    wasDisabled,
                                                    patchCount: patchedKeys.length,
                                                    patchedKeys: patchedKeys.slice(0, 20),
                                                    setupRefs,
                                                    compCount,
                                                    diagApp,
                                                    capturedFetches: _capturedFetches,
                                                    capturedXhr: _capturedXhr,
                                                    btnX: _btnX40,
                                                    btnY: _btnY40,
                                                    veiFound: _veiFound,
                                                    veiCalled: _veiCalled,
                                                    veiDepth: _veiDepth,
                                                    compCalled: _compCalled,
                                                    compMethod: _compMethod,
                                                    btnProtoPatched: _btnProtoPatched, // Fix44
                                                    pinaStoreKeys: _pinaStoreKeys,
                                                    pinaSubmitCalled: _pinaSubmitCalled,
                                                    pinaSubmitMethod: _pinaSubmitMethod,
                                                    pinaDisabledKeys: _pinaDisabledKeys,
                                                    fix49Called: _fix49Called,
                                                    fix49CallMethod: _fix49CallMethod,
                                                    fix49VxActions: _fix49VxActions,
                                                    fix49VxStateKeys: _fix49VxStateKeys,
                                                    fix49VxDispatch: _fix49VxDispatch,
                                                    // Fix51: store.getters proxy log (after first click)
                                                    getterAccess51: _getterAccess51,
                                                    getterPatched51: _getterPatched51,
                                                    dispatchLog51: _dispatchLog51,
                                                });
                                            });
                                        });
                                    });
                                }
                            """)
                            if _js_clicked and _js_clicked.get('clicked'):
                                _was_dis = _js_clicked.get('wasDisabled', False)
                                _pc = _js_clicked.get('patchCount', 0)
                                _pk = _js_clicked.get('patchedKeys', [])
                                _sr = _js_clicked.get('setupRefs', [])
                                _cc = _js_clicked.get('compCount', 0)
                                _da = _js_clicked.get('diagApp', {})
                                _cf = _js_clicked.get('capturedFetches', [])
                                _cx = _js_clicked.get('capturedXhr', [])
                                _bx = _js_clicked.get('btnX')
                                _by = _js_clicked.get('btnY')
                                _vf = _js_clicked.get('veiFound', False)
                                _vc = _js_clicked.get('veiCalled', False)
                                _vd = _js_clicked.get('veiDepth', -1)
                                _cca = _js_clicked.get('compCalled', False)
                                _ccm = _js_clicked.get('compMethod', '')
                                logger.info(f"  [Fix31/38] JS BFS click 成功 (wasDisabled={_was_dis}, patched={_pc}, comps={_cc})")
                                logger.info(f"  [Fix31/38] パッチしたキー(先20): {_pk}")
                                logger.info(f"  [Fix38] setupState refs patched: {_sr}")
                                logger.info(f"  [Fix31/38] App診断: {_da}")
                                _bpp = _js_clicked.get('btnProtoPatched', False)
                                logger.info(f"  [Fix42] veiFound={_vf}, veiCalled={_vc}, veiDepth={_vd}")
                                logger.info(f"  [Fix43c] compCalled={_cca}, compMethod={_ccm}")
                                logger.info(f"  [Fix44] btnProtoPatched={_bpp}")
                                logger.info(f"  [Fix39] capturedFetches: {_cf}")
                                logger.info(f"  [Fix39] capturedXhr: {_cx}")
                                _psk = _js_clicked.get('pinaStoreKeys', [])
                                _psc = _js_clicked.get('pinaSubmitCalled', False)
                                _psm = _js_clicked.get('pinaSubmitMethod', '')
                                _pdk = _js_clicked.get('pinaDisabledKeys', [])
                                logger.info(f"  [Fix46] pinaStores={_psk}")
                                logger.info(f"  [Fix46] pinaSubmitCalled={_psc}, method={_psm}")
                                logger.info(f"  [Fix46] pinaDisabledKeys={_pdk}")
                                # Fix49 diagnostics
                                _f49c = _js_clicked.get('fix49Called', False)
                                _f49m = _js_clicked.get('fix49CallMethod', '')
                                _f49va = _js_clicked.get('fix49VxActions', [])
                                _f49sk = _js_clicked.get('fix49VxStateKeys', [])
                                _f49vd = _js_clicked.get('fix49VxDispatch', '')
                                _da49m = _da.get('fix49Methods', []) if isinstance(_da, dict) else []
                                _da49c = _da.get('fix49Comp', False) if isinstance(_da, dict) else False
                                logger.info(f"  [Fix49] disabledConfirmComp={_da49c}, methods({len(_da49m)}): {_da49m[:30]}")
                                logger.info(f"  [Fix49] compMethodCalled={_f49c}, method={_f49m}")
                                logger.info(f"  [Fix49b] vxActions({len(_f49va)}): {_f49va}")
                                logger.info(f"  [Fix49b] vxStateKeys: {_f49sk}")
                                logger.info(f"  [Fix49b] vxDispatch={_f49vd}")
                                # Fix51: store.getters proxy log
                                _ga51 = _js_clicked.get('getterAccess51', {})
                                _gp51 = _js_clicked.get('getterPatched51', [])
                                _dl51 = _js_clicked.get('dispatchLog51', [])
                                _bool_true51 = {k: v for k, v in _ga51.items() if isinstance(v, bool) and v}
                                logger.info(f"  [Fix51] getter accessed ({len(_ga51)}): {list(_ga51.keys())[:30]}")
                                logger.info(f"  [Fix51] boolTrue getters (BLOCKER candidates): {_bool_true51}")
                                logger.info(f"  [Fix51] gettersPatched: {_gp51}")
                                logger.info(f"  [Fix51] dispatchLog: {_dl51}")
                                # Fix40: isTrusted=true フォールバック
                                # Fix42 が _vei 経由でハンドラを呼んだ場合は不要だが、
                                # フォールバックとして座標クリックも試みる。
                                if _bx and _by:
                                    _human_wait(0.2, 0.3)
                                    self._page.mouse.click(_bx, _by)
                                    logger.info(f"  [Fix40] page.mouse.click ({_bx:.0f}, {_by:.0f}) — isTrusted=true (fallback)")
                                    _human_wait(0.5, 0.8)
                            else:
                                _reason = _js_clicked.get('reason', '?') if _js_clicked else '?'
                                # 最終手段: force=True (旧フォールバック)
                                logger.info(f"  [Fix31] JS click 失敗({_reason}) → force=True 最終手段")
                                try:
                                    btn.click(force=True, timeout=5000)
                                except Exception:
                                    logger.warning("  force=True も失敗")
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
        # Fix47: btn.click() の代わりに bounding rect を取得して page.mouse.click()
        #         EDS button の内部 props.disabled チェックを bypass するため
        _human_wait(1, 2)
        try:
            dialog_rect = self._page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        if (btn.textContent.trim() === 'Save and Publish' && btn.offsetParent !== null) {
                            const rect = btn.getBoundingClientRect();
                            // ダイアログは画面中央付近（y:200-600, x:400-900）
                            if (rect.top > 150 && rect.top < 600 && rect.left > 300) {
                                // Fix47: removeAttribute + props.disabled patch もここで適用
                                try {
                                    btn.removeAttribute('disabled');
                                    btn.classList.remove('eds-button--disabled');
                                    btn.classList.remove('is-disabled');
                                    btn.style.pointerEvents = 'auto';
                                } catch(_) {}
                                return {
                                    found: true,
                                    cx: Math.round(rect.left + rect.width / 2),
                                    cy: Math.round(rect.top + rect.height / 2),
                                };
                            }
                        }
                    }
                    return {found: false};
                }
            """)
            if dialog_rect and dialog_rect.get('found'):
                _dx = dialog_rect['cx']
                _dy = dialog_rect['cy']
                logger.info(f"  [Fix47] 確認ダイアログ検出 → mouse.click({_dx}, {_dy})")
                # Fix52: Python-side request listener — capture ALL POST requests during entire dialog sequence
                _all_posts_52 = []
                def _on_request_52(request):
                    if request.method in ('POST', 'PUT', 'PATCH'):
                        _all_posts_52.append({'url': request.url[:200], 'method': request.method})
                self._page.on('request', _on_request_52)
                logger.info("  [Fix52] Python request listener installed")
                # Fix25: mouse.click() → Playwright locator click（座標ベースより信頼性が高い）
                # ダイアログ内の "Save and Publish" を locator で特定してクリック
                _f25_done = False
                for _f25_sel in [
                    '[role="dialog"] button:has-text("Save and Publish")',
                    '[class*="modal"] button:has-text("Save and Publish")',
                    '[class*="dialog"] button:has-text("Save and Publish")',
                    '[class*="overlay"] button:has-text("Save and Publish")',
                    '[class*="popup"] button:has-text("Save and Publish")',
                    '[class*="confirm"] button:has-text("Save and Publish")',
                ]:
                    try:
                        _f25_loc = self._page.locator(_f25_sel).first
                        if _f25_loc.count() and _f25_loc.is_visible(timeout=500):
                            _f25_loc.click(timeout=5000)
                            logger.info(f"  [Fix25] 確認ダイアログ locator click 成功: {_f25_sel}")
                            _f25_done = True
                            break
                    except Exception:
                        continue
                if not _f25_done:
                    logger.warning(f"  [Fix25] locator NG → mouse.click fallback ({_dx},{_dy})")
                    self._page.mouse.click(_dx, _dy)
                _human_wait(2, 3)

                # Fix50: store.getters をプロキシして実際のガード名を特定してパッチする
                # ダイアログ確認後のボタンクリックで、どのVuexゲッターが true を返して submit をブロックするかを特定する
                try:
                    _fix50_result = self._page.evaluate("""
                        async () => {
                            const _app50 = document.querySelector('[data-v-app]').__vue_app__;
                            const _proxy50 = _app50 && _app50._instance && _app50._instance.proxy;
                            const _store50 = _proxy50 && _proxy50.$store;

                            const _getterAccess = {};
                            const _dispatchCalls = [];
                            const _patchedGetters = [];

                            if (_store50) {
                                // store.getters をプロキシして全アクセスをロギング + true を返すものをパッチ
                                try {
                                    const _origGetters50 = _store50.getters;
                                    const _pxGetters50 = new Proxy(_origGetters50, {
                                        get(target, prop) {
                                            const val = Reflect.get(target, prop);
                                            if (typeof prop === 'string' && prop !== '__v_isReadonly' && prop !== '__v_isRef') {
                                                _getterAccess[prop] = val;
                                                // boolean true のゲッターをパッチ
                                                if (typeof val === 'boolean' && val === true) {
                                                    try {
                                                        Object.defineProperty(target, prop, { get: () => false, configurable: true });
                                                        _patchedGetters.push(prop);
                                                    } catch(_) {}
                                                }
                                            }
                                            return (typeof val === 'boolean' && val === true) ? false : val;
                                        }
                                    });
                                    Object.defineProperty(_store50, 'getters', { get: () => _pxGetters50, configurable: true });
                                } catch(_e50g) {}

                                // store.dispatch をプロキシして呼び出しをロギング
                                try {
                                    const _origDispatch50 = _store50.dispatch;
                                    _store50.dispatch = function(type, payload) {
                                        _dispatchCalls.push(String(type));
                                        return _origDispatch50.call(_store50, type, payload);
                                    };
                                } catch(_) {}

                                // store.state 直アクセスで save/disabled 系キーを探す
                                try {
                                    const _stateModuleKeys = Object.getOwnPropertyNames(_store50.state || {});
                                    const _stateReflectKeys = Reflect.ownKeys(_store50.state || {}).filter(k => typeof k === 'string');
                                    _getterAccess['__stateKeys'] = [...new Set([..._stateModuleKeys, ..._stateReflectKeys])].slice(0, 20);
                                } catch(_) {}

                                // _getters (内部 raw getters) を Reflect で列挙
                                try {
                                    const _gRaw = _store50._getters || _store50._wrappedGetters;
                                    if (_gRaw) {
                                        _getterAccess['__getterKeys'] = Reflect.ownKeys(_gRaw).filter(k => typeof k === 'string').slice(0, 40);
                                    }
                                } catch(_) {}
                            }

                            // メインボタン（bottom bar y>600）を探してクリック
                            const _allBtns50 = [...document.querySelectorAll('button')];
                            const _mainBtn50 = _allBtns50.find(b => {
                                const t = b.textContent.trim();
                                if (t !== 'Save and Publish') return false;
                                const r = b.getBoundingClientRect();
                                return r.top > 600;
                            });
                            let _clicked50 = false;
                            if (_mainBtn50) {
                                _mainBtn50.removeAttribute('disabled');
                                _mainBtn50.classList.remove('eds-button--disabled');
                                _mainBtn50.style.pointerEvents = 'auto';
                                _mainBtn50.click();
                                _clicked50 = true;
                            }

                            await new Promise(r => setTimeout(r, 3000));

                            return {
                                clicked: _clicked50,
                                boolTrueGetters: Object.entries(_getterAccess)
                                    .filter(([k, v]) => typeof v === 'boolean' && v === true)
                                    .map(([k]) => k).slice(0, 30),
                                allAccessedGetters: Object.keys(_getterAccess).filter(k => !k.startsWith('__')).slice(0, 60),
                                patchedGetters: _patchedGetters,
                                dispatchCalls: _dispatchCalls,
                                stateKeys: _getterAccess['__stateKeys'] || [],
                                getterKeys: _getterAccess['__getterKeys'] || [],
                            };
                        }
                    """)
                    logger.info(f"  [Fix50] store.getters proxy 結果: {_fix50_result}")
                except Exception as _f50e:
                    logger.warning(f"  [Fix50] エラー: {_f50e}")

                _human_wait(2, 3)

                # Fix49: ダイアログ後 BFS 完全再実行 + submit メソッド直接呼び出し + 3回目クリック
                # ダイアログ確認後に Vue が再レンダリングして ctx が新しいオブジェクトになる可能性があるため
                # BFS を再実行してパッチし直す。また disabledConfirm コンポーネントの全メソッドを試行。
                _fix49_result = self._page.evaluate("""
                    async () => {
                        const _captured = [];
                        const _consoleErrors = [];
                        // Spy on console.error to catch handler errors
                        const _origCE = console.error;
                        console.error = function(...args) { _consoleErrors.push(args.map(String).join(' ').slice(0, 200)); _origCE.apply(console, args); };
                        // Spy on window.onerror
                        const _origOE = window.onerror;
                        const _jsErrors = [];
                        window.onerror = function(msg, src, line, col, err) { _jsErrors.push(String(msg).slice(0, 200)); if (_origOE) _origOE.apply(window, arguments); };
                        // Spy on unhandledrejection
                        const _rejections = [];
                        function _onRej(e) { _rejections.push(String(e.reason).slice(0, 200)); }
                        window.addEventListener('unhandledrejection', _onRej);
                        const _orig = window.fetch;
                        window.fetch = function(...args) {
                            const url = String(args[0] && (args[0].url || args[0])).slice(0, 150);
                            const method = (args[1] && args[1].method) || 'GET';
                            const body = (args[1] && typeof args[1].body === 'string') ? args[1].body.slice(0, 300) : '?';
                            _captured.push({ url, method, body });
                            return _orig.apply(this, args);
                        };

                        const d49 = {
                            bfsComp: 0, bfsPatch: 0, bfsPatchedKeys: [],
                            submitCompFound: false, submitCompMethods: [],
                            submitMethodCalled: false, submitMethodName: '',
                            vxActions: [], vxStateKeys: [], vxDispatch: '', vxDispatchErr: '',
                            mainBtnFound: false, clicked3: false,
                            veiCalled3: false,
                        };

                        // ── BFS 再実行（ダイアログ後の再レンダリング対応）──
                        let _sc49 = null;  // disabledConfirm コンポーネント
                        const _pk49 = [];

                        function patchComp49(inst) {
                            try {
                                if (inst.props && inst.props.disabled === true) {
                                    Object.defineProperty(inst.props, 'disabled', { get: () => false, configurable: true });
                                    _pk49.push('props.disabled');
                                }
                            } catch(_) {}
                            const _ctx49 = inst.ctx;
                            if (_ctx49 && typeof _ctx49 === 'object') {
                                let _d49;
                                try { _d49 = Object.getOwnPropertyDescriptors(_ctx49); } catch(_) {}
                                if (_d49) {
                                    for (const k of Object.keys(_d49)) {
                                        const desc = _d49[k];
                                        if (typeof desc.get === 'function') {
                                            try {
                                                const cv = desc.get.call(_ctx49);
                                                if (typeof cv === 'boolean' && cv === true) {
                                                    Object.defineProperty(_ctx49, k, { get: () => false, configurable: true });
                                                    _pk49.push(`ctx.get:${k}`);
                                                    if (k === 'disabledConfirm' && !_sc49) _sc49 = inst;
                                                }
                                            } catch(_) {}
                                        }
                                    }
                                }
                            }
                            // setupState ComputedRef パッチ
                            const ss = inst.setupState;
                            if (ss && typeof ss === 'object') {
                                let rawSs = ss;
                                try { if (ss.__v_raw) rawSs = ss.__v_raw; } catch(_) {}
                                try {
                                    for (const k of Object.keys(rawSs)) {
                                        const val = rawSs[k];
                                        if (val && val.__v_isRef === true && typeof val._value === 'boolean' && val._value === true) {
                                            val._value = false;
                                            try { Object.defineProperty(val, 'value', { get: () => false, configurable: true }); } catch(_) {}
                                            _pk49.push(`setup:ref:${k}`);
                                        }
                                    }
                                } catch(_) {}
                            }
                        }

                        const rootEl = document.querySelector('[data-v-app]') || document.getElementById('app');
                        const vApp49 = rootEl && rootEl.__vue_app__;
                        if (vApp49 && vApp49._instance) {
                            const visited49 = new Set();
                            const queue49 = [vApp49._instance];
                            function walkV49(vnode) {
                                if (!vnode || typeof vnode !== 'object') return;
                                if (vnode.component && !visited49.has(vnode.component)) queue49.push(vnode.component);
                                if (Array.isArray(vnode.children)) {
                                    for (const c of vnode.children) walkV49(c);
                                } else if (vnode.children && typeof vnode.children === 'object') {
                                    for (const fn of Object.values(vnode.children)) {
                                        if (typeof fn === 'function') {
                                            try { const sv = fn(); if (Array.isArray(sv)) sv.forEach(walkV49); } catch(_) {}
                                        }
                                    }
                                }
                            }
                            while (queue49.length > 0 && d49.bfsComp < 2000) {
                                const comp = queue49.shift();
                                if (!comp || visited49.has(comp)) continue;
                                visited49.add(comp);
                                d49.bfsComp++;
                                patchComp49(comp);
                                if (comp.subTree) walkV49(comp.subTree);
                            }
                            d49.bfsPatch = _pk49.length;
                            d49.bfsPatchedKeys = _pk49.slice(0, 30);
                        }

                        // ── disabledConfirm コンポーネントのメソッドを列挙して呼ぶ ──
                        if (_sc49) {
                            d49.submitCompFound = true;
                            const _methods49 = [];
                            for (const obj of [_sc49.ctx, _sc49.proxy, _sc49.setupState].filter(Boolean)) {
                                try {
                                    const ks = [...new Set([...Object.keys(obj), ...Object.getOwnPropertyNames(obj)])];
                                    for (const k of ks) {
                                        try { if (typeof obj[k] === 'function' && !_methods49.includes(k)) _methods49.push(k); } catch(_) {}
                                    }
                                } catch(_) {}
                            }
                            d49.submitCompMethods = _methods49.slice(0, 60);

                            // submit 系メソッドを呼ぶ（広めのパターンで）
                            const _re49 = /save|submit|publish|confirm/i;
                            for (const obj of [_sc49.ctx, _sc49.proxy].filter(Boolean)) {
                                if (d49.submitMethodCalled) break;
                                for (const k of _methods49) {
                                    if (_re49.test(k) && !d49.submitMethodCalled) {
                                        try { obj[k](); d49.submitMethodCalled = true; d49.submitMethodName = k; } catch(_) {}
                                    }
                                }
                            }
                        }

                        // ── Vuex store Reflect.ownKeys 深部探索 ──
                        if (vApp49 && vApp49._instance && vApp49._instance.proxy) {
                            try {
                                const _s49 = vApp49._instance.proxy.$store;
                                if (_s49) {
                                    try { d49.vxActions = Reflect.ownKeys(_s49._actions || {}).filter(k => typeof k === 'string').slice(0, 40); } catch(_) {}
                                    try {
                                        const _raw = _s49._state && _s49._state.data;
                                        if (_raw) d49.vxStateKeys = Reflect.ownKeys(_raw).filter(k => typeof k === 'string').slice(0, 20);
                                    } catch(_) {}
                                    // 既知アクション名でディスパッチ
                                    const _savePat = /save|submit|publish/i;
                                    const _tryActs = [
                                        ...d49.vxActions.filter(k => _savePat.test(k)).slice(0, 5),
                                        'listing/submit', 'listing/saveAndPublish', 'listing/save',
                                        'product/submit', 'product/save', 'addProduct/save',
                                    ];
                                    for (const act of _tryActs) {
                                        if (d49.vxDispatch) break;
                                        try { await _s49.dispatch(act); d49.vxDispatch = act; } catch(e) { d49.vxDispatchErr = String(e).slice(0, 80); }
                                    }
                                }
                            } catch(_) {}
                        }

                        // ── メインボタン（bottom bar y>600）を再クリック ──
                        const _allBtns49 = [...document.querySelectorAll('button')];
                        const mainBtn49 = _allBtns49.find(b => {
                            const t = b.textContent.trim();
                            if (t !== 'Save and Publish') return false;
                            const r = b.getBoundingClientRect();
                            return r.top > 600;
                        });
                        d49.mainBtnFound = !!mainBtn49;
                        if (mainBtn49) {
                            mainBtn49.removeAttribute('disabled');
                            mainBtn49.classList.remove('eds-button--disabled');
                            mainBtn49.classList.remove('is-disabled');
                            mainBtn49.removeAttribute('aria-disabled');
                            mainBtn49.style.pointerEvents = 'auto';

                            // _vei ハンドラ直接呼び出し
                            let _v49el = mainBtn49;
                            for (let _vi49 = 0; _vi49 < 15 && _v49el; _vi49++, _v49el = _v49el.parentElement) {
                                const _vei49 = _v49el._vei;
                                if (!_vei49) continue;
                                const _h49 = _vei49.onClick || _vei49['onClick'];
                                const _fn49 = _h49 && (_h49.value || _h49);
                                if (typeof _fn49 === 'function') {
                                    const _ev49 = new MouseEvent('click', { bubbles: true, cancelable: true, composed: true, view: window });
                                    _fn49(_ev49);
                                    d49.veiCalled3 = true;
                                    d49.clicked3 = true;
                                    break;
                                }
                            }
                            if (!d49.veiCalled3) {
                                mainBtn49.click();
                                d49.clicked3 = true;
                            }
                        }

                        await new Promise(r => setTimeout(r, 5000));
                        window.fetch = _orig;
                        console.error = _origCE;
                        window.onerror = _origOE;
                        window.removeEventListener('unhandledrejection', _onRej);
                        return { d49, fetches: _captured, consoleErrors: _consoleErrors, jsErrors: _jsErrors, rejections: _rejections };
                    }
                """)
                logger.info(f"  [Fix49] BFS再実行+3回目クリック結果: {_fix49_result.get('d49') if _fix49_result else None}")
                # Fix52b: JS-side error spies
                _ce52 = _fix49_result.get('consoleErrors', []) if _fix49_result else []
                _je52 = _fix49_result.get('jsErrors', []) if _fix49_result else []
                _rj52 = _fix49_result.get('rejections', []) if _fix49_result else []
                logger.info(f"  [Fix52b] consoleErrors: {_ce52}")
                logger.info(f"  [Fix52b] jsErrors: {_je52}")
                logger.info(f"  [Fix52b] unhandledRejections: {_rj52}")
                _f49fetches = _fix49_result.get('fetches', []) if _fix49_result else []
                logger.info(f"  [Fix49] capturedFetches(3rd): {_f49fetches}")

                # Fix52: remove listener + log ALL POST/PUT/PATCH requests captured since dialog click
                try:
                    self._page.remove_listener('request', _on_request_52)
                except Exception:
                    pass
                logger.info(f"  [Fix52] ALL POST/PUT/PATCH captured ({len(_all_posts_52)}): {_all_posts_52}")
                # Categorize: analytics vs submit candidates
                _submit_cands_52 = [r for r in _all_posts_52 if 'listing-upload/submit' in r['url'] or 'listing/submit' in r['url'] or 'product/submit' in r['url'] or 'create_product_info' in r['url'] or 'add_product' in r['url'] or 'save_product' in r['url']]
                _analytics_52 = [r for r in _all_posts_52 if 'web-performance' in r['url'] or 'analytics' in r['url'] or 'lscmpt' in r['url']]
                logger.info(f"  [Fix52] submit candidates: {_submit_cands_52}")
                logger.info(f"  [Fix52] analytics (excluded): {len(_analytics_52)}")
                _other_52 = [r for r in _all_posts_52 if r not in _submit_cands_52 and r not in _analytics_52]
                logger.info(f"  [Fix52] other (non-analytics, non-submit): {_other_52}")

                # Fix47b: $store Reflect.ownKeys 診断（補足）
                try:
                    store_diag = self._page.evaluate("""
                        () => {
                            const diag = {storeFound: false, vxActionsReflect: [], vxStateReflect: []};
                            try {
                                const app = document.querySelector('[data-v-app]').__vue_app__;
                                const proxy = app._instance && app._instance.proxy;
                                if (proxy && proxy.$store) {
                                    const store = proxy.$store;
                                    diag.storeFound = true;
                                    try { diag.vxActionsReflect = Reflect.ownKeys(store._actions || {}).filter(k => typeof k === 'string').slice(0, 40); } catch(_) {}
                                    try {
                                        const raw = store._state && store._state.data;
                                        if (raw) diag.vxStateReflect = Reflect.ownKeys(raw).filter(k => typeof k === 'string').slice(0, 20);
                                    } catch(_) {}
                                }
                            } catch(e) { diag.err = String(e).slice(0, 100); }
                            return diag;
                        }
                    """)
                    logger.info(f"  [Fix47b] $store Reflect診断: {store_diag}")
                except Exception as _se:
                    logger.warning(f"  [Fix47b] $store診断エラー: {_se}")
                _human_wait(3, 5)
            else:
                logger.info("  確認ダイアログ: 検出されず（スキップ）")
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
