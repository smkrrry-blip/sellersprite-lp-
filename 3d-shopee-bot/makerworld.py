"""
MakerWorld スクレイパー
makerworld.com から商品データ（画像・説明・レビュー）を取得する

戦略:
  1. 内部API（/api/v1/...）を直接呼ぶ（最速）
  2. 失敗時はPlaywrightで描画してからDOM解析
"""
import re
import time
import json
import random
import logging
import requests
from typing import Optional
from config import SCRAPING_SETTINGS, MAKERWORLD_EMAIL, MAKERWORLD_PASSWORD

logger = logging.getLogger(__name__)

BASE_URL = "https://makerworld.com"
API_BASE = "https://makerworld.com/api/v1"

# MakerWorld の実際のページ構造に基づくURL
MW_MODELS_URL   = f"{BASE_URL}/en/models"          # /en/models?sortBy=likes&pageSize=20
MW_SEARCH_URL   = f"{BASE_URL}/en/models"          # ?keyword=xxx&sortBy=likes
MW_MODEL_URL    = f"{BASE_URL}/en/models"          # /en/models/{id}

# 内部APIエンドポイント（優先順）
API_ENDPOINTS_SEARCH = [
    f"{BASE_URL}/api/v1/design/page",
    f"{BASE_URL}/api/v1/design/list",
    f"{BASE_URL}/api/v1/model/page",
    f"{BASE_URL}/api/v2/design/page",
]

API_ENDPOINTS_DETAIL = [
    f"{API_BASE}/design",          # GET /api/v1/design/{id}
    f"{BASE_URL}/api/v1/model",    # GET /api/v1/model/{id}
]

# ブラウザに偽装するヘッダー
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8,th;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://makerworld.com/en",
    "Origin": "https://makerworld.com",
    "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# 商業利用可ライセンスのキーワード
COMMERCIAL_OK_LICENSES = [
    "CC0", "CC BY", "CC BY-SA",
    "Creative Commons Zero",
    "Creative Commons Attribution",
    "Public Domain",
]

# 商業利用不可ライセンス
COMMERCIAL_NG_LICENSES = [
    "CC BY-NC", "CC BY-NC-SA", "CC BY-NC-ND",
    "No Derivatives", "Personal Use",
]


class MakerWorldScraper:

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._auth_token: Optional[str] = None

    def _sleep(self):
        """レート制限対策のランダム待機"""
        t = random.uniform(
            SCRAPING_SETTINGS["delay_min_sec"],
            SCRAPING_SETTINGS["delay_max_sec"]
        )
        time.sleep(t)

    def _get(self, url: str, params: dict = None) -> Optional[dict]:
        """GETリクエスト（リトライ付き）"""
        for attempt in range(SCRAPING_SETTINGS["max_retries"]):
            try:
                resp = self.session.get(url, params=params, timeout=15)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    logger.warning(f"Rate limited. Waiting 30s...")
                    time.sleep(30)
                else:
                    logger.warning(f"HTTP {resp.status_code} for {url}")
            except Exception as e:
                logger.error(f"Request error (attempt {attempt+1}): {e}")
                time.sleep(5)
            self._sleep()
        return None

    def login(self) -> bool:
        """MakerWorldにログイン（認証が必要なデータ取得用）"""
        if not MAKERWORLD_EMAIL or not MAKERWORLD_PASSWORD:
            logger.info("MakerWorld credentials not set. Using public access only.")
            return False
        try:
            resp = self.session.post(f"{API_BASE}/user/login", json={
                "email": MAKERWORLD_EMAIL,
                "password": MAKERWORLD_PASSWORD,
            }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token") or data.get("access_token")
                if token:
                    self._auth_token = token
                    self.session.headers["Authorization"] = f"Bearer {token}"
                    logger.info("✅ MakerWorld ログイン成功")
                    return True
        except Exception as e:
            logger.error(f"Login error: {e}")
        return False

    def search_models(
        self,
        keyword: str = "",
        category: str = "",
        sort_by: str = "likes",
        page: int = 1,
        per_page: int = 20,
    ) -> list[dict]:
        """
        MakerWorldで商品を検索
        sort_by: "likes", "downloads", "makes", "newest"
        MakerWorld実際のパラメータ: sortBy, pageSize, page, keyword, categoryId
        """
        # 実際のMakerWorldページ構造に合わせたパラメータ
        params = {
            "keyword": keyword,
            "sortBy": sort_by,          # MakerWorld実際のパラメータ名
            "pageSize": per_page,       # pageSize（limitではなく）
            "page": page,
            "offset": (page - 1) * per_page,
            "limit": per_page,          # 旧APIとの互換性
        }
        if category:
            params["category"] = category
            params["categoryId"] = category  # 両方試す

        for endpoint in API_ENDPOINTS_SEARCH:
            data = self._get(endpoint, params)
            if data:
                models = (
                    data.get("hits") or
                    data.get("models") or
                    data.get("designs") or
                    data.get("data", {}).get("list") or
                    data.get("list") or
                    []
                )
                if models:
                    logger.info(f"✅ {len(models)}件取得 from {endpoint}")
                    return [self._normalize_model(m) for m in models]
            self._sleep()

        # API失敗時はPlaywrightフォールバック
        logger.warning("Internal API failed. Trying Playwright...")
        return self._search_via_playwright(keyword, sort_by, page, per_page)

    def _search_via_playwright(
        self, keyword: str, sort_by: str, page: int, per_page: int
    ) -> list[dict]:
        """
        Playwrightを使ったフォールバックスクレイピング
        MakerWorldの実際のURL: /en/models?sortBy=likes&pageSize=20&keyword=xxx
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
            return []

        models = []
        # MakerWorldの実際のURL構造: /en/models（検索もここ）
        import urllib.parse
        query_params = {"sortBy": sort_by, "pageSize": str(per_page), "page": str(page)}
        if keyword:
            query_params["keyword"] = keyword
        search_url = f"{MW_MODELS_URL}?{urllib.parse.urlencode(query_params)}"
        logger.info(f"Playwright: {search_url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )

            # /bff/ と /api/ 両方のAPIレスポンスをインターセプト
            api_responses = []

            def handle_response(response):
                if response.status != 200:
                    return
                if not any(p in response.url for p in ["/bff/", "/api/"]):
                    return
                try:
                    body = response.json()
                    items = (
                        body.get("hits") or
                        body.get("models") or
                        body.get("designs") or
                        (body.get("data") or {}).get("list") or
                        body.get("list") or
                        []
                    )
                    if items:
                        api_responses.insert(0, {"url": response.url, "body": body, "items": items})
                        logger.info(f"API intercepted: {len(items)}件 from {response.url}")
                    else:
                        api_responses.append({"url": response.url, "body": body, "items": []})
                except Exception:
                    pass

            page_obj = context.new_page()
            page_obj.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page_obj.on("response", handle_response)

            # domcontentloaded で待機（networkidle はCloudflareで無限待機になる）
            try:
                page_obj.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning(f"goto timeout（続行）: {e}")

            # Cloudflare通過 + コンテンツ描画を待つ
            time.sleep(5)
            try:
                page_obj.wait_for_selector(
                    "a[href*='/en/models/'], [class*='model'], [class*='card']",
                    timeout=15000,
                )
            except Exception:
                pass
            time.sleep(3)

            # インターセプトしたAPIレスポンスから商品取得
            for resp in api_responses:
                if resp["items"]:
                    models = [self._normalize_model(m) for m in resp["items"]]
                    logger.info(f"✅ API経由: {len(models)}件")
                    break

            # APIレスポンスが取れなかった場合DOMから取得
            if not models:
                logger.info("DOM解析にフォールバック...")
                models = self._parse_dom(page_obj)

            browser.close()

        return models

    def _parse_dom(self, page) -> list[dict]:
        """
        DOMから商品カードを解析
        MakerWorldの実際のDOM構造に対応:
          - <a href="/en/models/{id}"> でリンクを取得
          - 画像は <img> または data-src 属性
        """
        models = []
        try:
            # MakerWorldのモデルカードセレクター候補
            selectors = [
                "a[href*='/en/models/']",       # モデルへのリンク（最も確実）
                "[class*='model-card']",
                "[class*='ModelCard']",
                "[class*='make-card']",
                "[data-model-id]",
            ]
            cards = []
            for sel in selectors:
                cards = page.query_selector_all(sel)
                if cards:
                    logger.info(f"DOM selector matched: {sel} ({len(cards)} cards)")
                    break

            seen_ids = set()
            for card in cards:
                try:
                    # href からmodel IDを抽出
                    href = card.get_attribute("href") or ""
                    if "/en/models/" in href:
                        model_id = href.rstrip("/").split("/en/models/")[-1].split("?")[0]
                    else:
                        model_id = card.get_attribute("data-model-id") or ""

                    if not model_id or model_id in seen_ids:
                        continue
                    seen_ids.add(model_id)

                    # タイトル取得
                    title_el = card.query_selector("h3, h2, [class*='title'], [class*='name'], [class*='label']")
                    title = title_el.inner_text().strip() if title_el else ""

                    # 画像取得（lazy-load対応: data-src / src）
                    img_el = card.query_selector("img")
                    img_url = ""
                    if img_el:
                        img_url = (
                            img_el.get_attribute("data-src") or
                            img_el.get_attribute("src") or ""
                        )

                    # いいね数取得（あれば）
                    like_el = card.query_selector("[class*='like'], [class*='heart'], [class*='favor']")
                    likes = 0
                    if like_el:
                        try:
                            likes = int(like_el.inner_text().strip().replace(",", "") or 0)
                        except ValueError:
                            pass

                    models.append({
                        "mw_model_id": model_id,
                        "title_en": title,
                        "mw_url": f"{MW_MODEL_URL}/{model_id}",
                        "image_urls": [img_url] if img_url else [],
                        "likes": likes,
                        "commercial_ok": 0,  # DOM解析では不明
                    })
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"DOM parse error: {e}")
        logger.info(f"DOM parse result: {len(models)} models")
        return models

    def get_model_detail(self, model_id: str) -> Optional[dict]:
        """
        特定商品の詳細情報を取得
        MakerWorld実際のURL: /en/models/{model_id}
        """
        for base_ep in API_ENDPOINTS_DETAIL:
            data = self._get(f"{base_ep}/{model_id}")
            if data and (data.get("id") or data.get("model_id") or data.get("design_id")):
                return self._normalize_model_detail(data)
            self._sleep()

        # Playwright フォールバック
        return self._get_detail_via_playwright(model_id)

    def _get_detail_via_playwright(self, model_id: str) -> Optional[dict]:
        """Playwrightで商品詳細を取得（MakerWorld /en/models/{id}）"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        model_url = f"{MW_MODEL_URL}/{model_id}"
        detail_data = {}

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            api_responses = []

            def handle_response(response):
                url = response.url
                if "/api/" in url and response.status == 200:
                    try:
                        body = response.json()
                        # IDが一致するレスポンスを最優先
                        if str(model_id) in url:
                            api_responses.insert(0, body)
                        else:
                            api_responses.append(body)
                    except Exception:
                        pass

            page.on("response", handle_response)
            page.goto(model_url, wait_until="networkidle", timeout=45000)
            time.sleep(2)

            if api_responses:
                detail_data = self._normalize_model_detail(api_responses[0])
            else:
                # DOM解析（フォールバック）
                try:
                    title_raw = page.title()
                    # "モデル名 | MakerWorld" のような形式から商品名だけ抽出
                    title = title_raw.split(" | ")[0].strip() if " | " in title_raw else title_raw

                    desc_el = page.query_selector(
                        "[class*='description'], [class*='detail'] p, "
                        "[class*='content'] p, main p"
                    )
                    desc = desc_el.inner_text().strip() if desc_el else ""

                    # MakerWorldの画像（CDNドメイン対応）
                    imgs = []
                    for img in page.query_selector_all("img"):
                        src = img.get_attribute("data-src") or img.get_attribute("src") or ""
                        if src and ("makerworld" in src or "bambu" in src or "cdn" in src):
                            imgs.append(src)

                    # ライセンス取得
                    license_el = page.query_selector("[class*='license'], [class*='License']")
                    license_str = license_el.inner_text().strip() if license_el else ""

                    detail_data = {
                        "mw_model_id": str(model_id),
                        "title_en": title,
                        "description_en": desc,
                        "image_urls": imgs[:8],
                        "mw_url": model_url,
                        "license": license_str,
                        "commercial_ok": 1 if self._check_commercial_license(license_str) else 0,
                    }
                except Exception as e:
                    logger.error(f"DOM detail parse error: {e}")

            browser.close()

        return detail_data or None

    def _normalize_model(self, raw: dict) -> dict:
        """APIレスポンスを統一フォーマットに変換"""
        model_id = (
            str(raw.get("id") or raw.get("model_id") or raw.get("design_id") or "")
        )
        title = (
            raw.get("title") or raw.get("name") or raw.get("model_name") or ""
        )
        images = (
            raw.get("images") or raw.get("image_list") or raw.get("cover_images") or []
        )
        if isinstance(images, list) and images and isinstance(images[0], dict):
            images = [img.get("url") or img.get("src") or "" for img in images]

        cover = raw.get("cover") or raw.get("thumbnail") or raw.get("preview_img") or ""
        if cover and cover not in images:
            images.insert(0, cover)

        tags = raw.get("tags") or raw.get("tag_list") or []
        if isinstance(tags, list) and tags and isinstance(tags[0], dict):
            tags = [t.get("name") or t.get("tag") or "" for t in tags]

        license_str = str(raw.get("license") or raw.get("license_type") or "")
        commercial_ok = self._check_commercial_license(license_str)

        return {
            "mw_model_id": model_id,
            "title_en": title,
            "description_en": raw.get("description") or raw.get("summary") or "",
            "mw_url": f"{BASE_URL}/en/models/{model_id}",
            "image_urls": [img for img in images if img][:8],
            "category": raw.get("category") or raw.get("category_name") or "",
            "tags": tags,
            "likes": int(raw.get("like_count") or raw.get("likes") or 0),
            "makes": int(raw.get("make_count") or raw.get("makes") or 0),
            "downloads": int(raw.get("download_count") or raw.get("downloads") or 0),
            "license": license_str,
            "commercial_ok": 1 if commercial_ok else 0,
            "print_weight_g": float(raw.get("weight") or 0),
            "print_hours": float(raw.get("print_time") or 0),
        }

    def _normalize_model_detail(self, raw: dict) -> dict:
        """詳細APIレスポンスを正規化（基本と同じ＋詳細情報）"""
        base = self._normalize_model(raw)
        # 詳細レビューを追加
        reviews = raw.get("reviews") or raw.get("makes") or []
        review_texts = []
        for r in reviews[:5]:
            text = r.get("content") or r.get("comment") or r.get("review") or ""
            if text:
                review_texts.append(str(text))
        base["reviews"] = review_texts
        base["print_settings"] = raw.get("print_settings") or {}
        return base

    def _check_commercial_license(self, license_str: str) -> bool:
        """ライセンスが商業利用可かチェック"""
        if not license_str:
            return False
        license_upper = license_str.upper()
        # 明確にNGなライセンス
        for ng in COMMERCIAL_NG_LICENSES:
            if ng.upper() in license_upper:
                return False
        # OKなライセンス
        for ok in COMMERCIAL_OK_LICENSES:
            if ok.upper() in license_upper:
                return True
        # 不明な場合は安全のためFalse
        return False

    def get_trending_models(self, limit: int = 100) -> list[dict]:
        """
        トレンド商品を一括取得
        商業利用可能なものだけをフィルタリング
        """
        all_models = []
        categories = ["", "Hobby & Crafts", "Home & Living", "Toys", "Education"]
        sorts = ["likes", "makes", "downloads"]

        for sort in sorts:
            for cat in categories:
                logger.info(f"Fetching: sort={sort}, category='{cat}'")
                models = self.search_models(
                    keyword="", category=cat, sort_by=sort, page=1, per_page=50
                )
                # フィルタリング
                filtered = [
                    m for m in models
                    if m.get("commercial_ok") and
                    m.get("likes", 0) >= SCRAPING_SETTINGS["min_likes"] and
                    m.get("makes", 0) >= SCRAPING_SETTINGS["min_makes"]
                ]
                all_models.extend(filtered)
                logger.info(f"  → {len(filtered)}/{len(models)} 件が条件通過")
                self._sleep()

                if len(all_models) >= limit:
                    break
            if len(all_models) >= limit:
                break

        # 重複排除
        seen = set()
        unique = []
        for m in all_models:
            if m["mw_model_id"] not in seen:
                seen.add(m["mw_model_id"])
                unique.append(m)

        logger.info(f"✅ 合計 {len(unique)} 件のユニーク商品を取得")
        return unique[:limit]

    def enrich_with_detail(self, models: list[dict]) -> list[dict]:
        """
        商品リストに詳細情報（レビュー・詳細説明）を追加
        """
        enriched = []
        for i, m in enumerate(models):
            logger.info(f"[{i+1}/{len(models)}] 詳細取得: {m['title_en']}")
            detail = self.get_model_detail(m["mw_model_id"])
            if detail:
                m.update(detail)
            enriched.append(m)
            self._sleep()
        return enriched


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    scraper = MakerWorldScraper()
    print("MakerWorldスクレイパーテスト")
    models = scraper.search_models(keyword="organizer", sort_by="likes", per_page=5)
    print(f"{len(models)}件取得:")
    for m in models:
        print(f"  [{m['mw_model_id']}] {m['title_en']} (♥{m['likes']} 🖨️{m['makes']}) CC={m['commercial_ok']}")
