"""
Shopee Open Platform API v2 クライアント
Thailand向け商品出品・画像アップロード・注文管理
"""
import io
import time
import hmac
import json
import hashlib
import logging
import requests
from pathlib import Path
from typing import Optional
from config import (
    SHOPEE_PARTNER_ID, SHOPEE_PARTNER_KEY,
    SHOPEE_SHOP_ID, SHOPEE_ACCESS_TOKEN,
    SHOPEE_ENV, SHOPEE_BASE_URL, LISTING_SETTINGS, CATEGORY_MAP,
    LOGISTICS_CHANNELS, DEFAULT_LOGISTICS_IDS,
)

logger = logging.getLogger(__name__)

BASE_URL = SHOPEE_BASE_URL.get(SHOPEE_ENV, SHOPEE_BASE_URL["production"])


class ShopeeAPI:

    def __init__(self):
        self.partner_id  = int(SHOPEE_PARTNER_ID) if SHOPEE_PARTNER_ID else 0
        self.partner_key = SHOPEE_PARTNER_KEY
        self.shop_id     = int(SHOPEE_SHOP_ID) if SHOPEE_SHOP_ID else 0
        self.access_token = SHOPEE_ACCESS_TOKEN
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    # ─── 署名生成 ────────────────────────────────────────

    def _sign(self, path: str, timestamp: int) -> str:
        """
        Shopee v2 HMAC-SHA256署名
        base_string = partner_id + path + timestamp + access_token + shop_id
        """
        base = (
            f"{self.partner_id}{path}{timestamp}"
            f"{self.access_token}{self.shop_id}"
        )
        return hmac.new(
            self.partner_key.encode("utf-8"),
            base.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _build_url(self, path: str) -> tuple[str, dict]:
        """URL + 認証パラメータを生成"""
        timestamp = int(time.time())
        sign = self._sign(path, timestamp)
        url = BASE_URL + path
        params = {
            "partner_id": self.partner_id,
            "timestamp": timestamp,
            "access_token": self.access_token,
            "shop_id": self.shop_id,
            "sign": sign,
        }
        return url, params

    def _post(self, path: str, body: dict) -> dict:
        url, params = self._build_url(path)
        try:
            resp = self.session.post(url, params=params, json=body, timeout=30)
            data = resp.json()
            if data.get("error"):
                logger.error(f"Shopee API Error [{path}]: {data.get('message')} (code: {data.get('error')})")
            return data
        except Exception as e:
            logger.error(f"Request error [{path}]: {e}")
            return {"error": str(e)}

    def _get(self, path: str, params_extra: dict = None) -> dict:
        url, params = self._build_url(path)
        if params_extra:
            params.update(params_extra)
        try:
            resp = self.session.get(url, params=params, timeout=30)
            return resp.json()
        except Exception as e:
            logger.error(f"GET error [{path}]: {e}")
            return {"error": str(e)}

    # ─── 画像アップロード ────────────────────────────────

    def upload_image(self, image_url: str) -> Optional[str]:
        """
        MakerWorldの画像URLをShopeeにアップロード
        → Shopee image_id を返す
        """
        try:
            # 画像をダウンロード
            img_resp = requests.get(image_url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ShopeeBot/1.0)"
            })
            if img_resp.status_code != 200:
                logger.warning(f"Image download failed: {image_url}")
                return None

            image_data = img_resp.content

            # Shopeeにアップロード（multipart/form-data）
            path = "/api/v2/media_space/upload_image"
            timestamp = int(time.time())
            sign = self._sign(path, timestamp)
            url = BASE_URL + path
            params = {
                "partner_id": self.partner_id,
                "timestamp": timestamp,
                "access_token": self.access_token,
                "shop_id": self.shop_id,
                "sign": sign,
            }

            # Content-Typeをmultipart用に変更
            resp = requests.post(
                url, params=params,
                files={"image": ("image.jpg", image_data, "image/jpeg")},
                timeout=30
            )
            data = resp.json()
            image_id = (
                data.get("response", {}).get("image_id") or
                data.get("image_id") or
                data.get("data", {}).get("image_id")
            )
            if image_id:
                logger.info(f"✅ 画像アップロード成功: {image_id}")
                return str(image_id)
            else:
                logger.warning(f"Upload response: {data}")
                return None

        except Exception as e:
            logger.error(f"Image upload error: {e}")
            return None

    def upload_images_batch(self, image_urls: list[str]) -> list[str]:
        """複数画像を順番にアップロード"""
        image_ids = []
        for url in image_urls[:9]:  # Shopeeは最大9枚
            img_id = self.upload_image(url)
            if img_id:
                image_ids.append(img_id)
            time.sleep(0.5)
        return image_ids

    # ─── カテゴリ取得 ──────────────────────────────────

    def get_category(self, language: str = "th") -> dict:
        """Shopee Thailandのカテゴリ一覧を取得"""
        data = self._get("/api/v2/product/get_category", {"language": language})
        return data

    def get_attributes(self, category_id: int) -> dict:
        """カテゴリの必須属性を取得"""
        data = self._get("/api/v2/product/get_attributes", {
            "category_id": category_id,
            "language": "th"
        })
        return data

    # ─── 商品出品 ──────────────────────────────────────

    def add_item(self, product: dict, image_ids: list[str]) -> Optional[str]:
        """
        Shopeeに商品を出品
        product: 正規化された商品データ（title_th, description_th, price_thb等）
        image_ids: Shopeeにアップロード済みの画像IDリスト
        → 成功時: item_id（文字列）
        """
        if not image_ids:
            logger.error("画像IDが空です。出品スキップ。")
            return None

        # カテゴリIDを解決
        category_id = CATEGORY_MAP.get(
            product.get("category", ""), CATEGORY_MAP["DEFAULT"]
        )

        # 価格と重量
        price = float(product.get("price_thb") or LISTING_SETTINGS["min_price_thb"])
        weight = float(product.get("estimated_grams", 100)) / 1000  # g → kg
        if weight < 0.01:
            weight = 0.1  # 最低100g

        # 商品名（タイ語・最大120文字）
        title = str(product.get("title_th") or product.get("title_en") or "3D Print Item")[:120]

        # 説明文（タイ語）
        description = str(product.get("description_th") or "สินค้า 3D Printed คุณภาพสูง")[:3000]

        body = {
            "category_id": category_id,
            "item_name": title,
            "description": description,
            "item_sku": f"MW-{product.get('mw_model_id', 'unknown')[:20]}",
            "condition": "NEW",
            "images": [{"image_id": img_id} for img_id in image_ids[:9]],
            "weight": weight,
            "dimension": {
                "package_length": 20,
                "package_width": 20,
                "package_height": 10,
            },
            "price_info": [{
                "currency": "THB",
                "original_price": price,
            }],
            "stock_info_v2": {
                "summary_info": {
                    "total_reserved_stock": 0,
                    "total_available_stock": LISTING_SETTINGS["default_stock"],
                },
                "seller_stock": [{
                    "stock": LISTING_SETTINGS["default_stock"]
                }]
            },
            "logistic_info": self._get_default_logistics(),
            "days_to_ship": LISTING_SETTINGS["default_days_to_ship"],
            "wholesales": [],
        }

        data = self._post("/api/v2/product/add_item", body)

        item_id = (
            data.get("response", {}).get("item_id") or
            data.get("item_id") or
            data.get("data", {}).get("item_id")
        )

        if item_id:
            logger.info(f"✅ 出品成功: item_id={item_id} / {title}")
            return str(item_id)
        else:
            err = data.get("message") or data.get("error") or "Unknown error"
            logger.error(f"❌ 出品失敗: {err}")
            return None

    def _get_default_logistics(self) -> list[dict]:
        """
        タイ標準配送設定（config.py の LOGISTICS_CHANNELS から生成）
        DEFAULT_LOGISTICS_IDS に含まれるチャンネルのみ有効化
        """
        result = []
        for ch in LOGISTICS_CHANNELS:
            if ch["logistic_id"] in DEFAULT_LOGISTICS_IDS:
                result.append({
                    "logistic_id": ch["logistic_id"],
                    "enabled": ch.get("enabled", True),
                    "is_free": ch.get("is_free", False),
                    "shipping_fee": ch.get("shipping_fee", 50.0),
                })
        # fallback: 設定がなければ Kerry Express を使用
        if not result:
            result = [{"logistic_id": 40009, "enabled": True, "is_free": False, "shipping_fee": 50.0}]
        return result

    # ─── 注文管理 ──────────────────────────────────────

    def get_order_list(self, time_from: int = None, time_to: int = None) -> list[dict]:
        """未発送の注文一覧を取得"""
        if not time_from:
            time_from = int(time.time()) - 86400 * 7  # 過去7日
        if not time_to:
            time_to = int(time.time())

        data = self._get("/api/v2/order/get_order_list", {
            "time_range_field": "create_time",
            "time_from": time_from,
            "time_to": time_to,
            "page_size": 50,
            "order_status": "READY_TO_SHIP",
        })
        return data.get("response", {}).get("order_list", [])

    def get_order_detail(self, order_sn: str) -> dict:
        """注文詳細を取得"""
        data = self._get("/api/v2/order/get_order_detail", {
            "order_sn_list": order_sn,
        })
        return data.get("response", {}).get("order_list", [{}])[0]

    def ship_order(self, order_sn: str, tracking_number: str, carrier: str = "Thai Post") -> bool:
        """注文を発送済みにする"""
        data = self._post("/api/v2/logistics/ship_order", {
            "order_sn": order_sn,
            "package_number": "",
            "pickup": {
                "pickup_time_id": "",
            },
            "dropoff": {
                "branch_id": 0,
                "sender_real_name": "",
                "tracking_no": tracking_number,
            }
        })
        success = not data.get("error")
        if success:
            logger.info(f"✅ 発送登録: {order_sn} / {tracking_number}")
        else:
            logger.error(f"❌ 発送登録失敗: {data.get('message')}")
        return success

    # ─── 既存商品管理 ─────────────────────────────────

    def get_item_list(self, offset: int = 0, limit: int = 100) -> list[dict]:
        """出品済み商品一覧を取得"""
        data = self._get("/api/v2/product/get_item_list", {
            "offset": offset,
            "page_size": limit,
            "item_status": "NORMAL",
        })
        return data.get("response", {}).get("item", [])

    def update_price(self, item_id: str, price: float) -> bool:
        """商品価格を更新"""
        data = self._post("/api/v2/product/update_price", {
            "item_id": int(item_id),
            "price_list": [{"model_id": 0, "original_price": price}]
        })
        return not data.get("error")

    def update_stock(self, item_id: str, stock: int) -> bool:
        """在庫数を更新"""
        data = self._post("/api/v2/product/update_stock", {
            "item_id": int(item_id),
            "stock_list": [{"model_id": 0, "seller_stock": [{"stock": stock}]}]
        })
        return not data.get("error")

    def get_shop_info(self) -> dict:
        """ショップ情報を取得（接続テスト用）"""
        data = self._get("/api/v2/shop/get_shop_info")
        return data.get("response", {})

    def test_connection(self) -> bool:
        """API接続テスト"""
        if not all([self.partner_id, self.partner_key, self.shop_id, self.access_token]):
            logger.error("❌ Shopee認証情報が未設定です。config.pyを確認してください。")
            logger.info("必要な情報:")
            logger.info("  - SHOPEE_PARTNER_ID")
            logger.info("  - SHOPEE_PARTNER_KEY")
            logger.info("  - SHOPEE_SHOP_ID")
            logger.info("  - SHOPEE_ACCESS_TOKEN")
            return False
        info = self.get_shop_info()
        if info.get("shop_name"):
            logger.info(f"✅ Shopee接続成功: {info['shop_name']} ({info.get('status')})")
            return True
        else:
            logger.error(f"❌ Shopee接続失敗: {info}")
            return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    api = ShopeeAPI()
    api.test_connection()
