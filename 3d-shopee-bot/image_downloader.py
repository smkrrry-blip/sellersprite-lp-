"""
MakerWorldから商品画像をダウンロードしてローカル保存するモジュール
保存先: ~/3d-shopee-bot/images/
フォーマット: JPEG（品質85）、最大800x800px
ファイル名: {mw_model_id}_{index}.jpg
"""
import logging
import time
import random
from pathlib import Path
from typing import Optional

import requests
from PIL import Image
import io

logger = logging.getLogger(__name__)

IMAGES_DIR = Path(__file__).parent / "images"
IMAGES_DIR.mkdir(exist_ok=True)

MAX_SIZE = (800, 800)
JPEG_QUALITY = 85

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://makerworld.com/",
}


def download_image(url: str, save_path: Path) -> bool:
    """
    1枚の画像をダウンロードしてリサイズ保存
    → 成功: True / 失敗: False
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"画像ダウンロード失敗 HTTP {resp.status_code}: {url}")
            return False

        # Pillowで開いてリサイズ
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img.thumbnail(MAX_SIZE, Image.LANCZOS)
        img.save(str(save_path), "JPEG", quality=JPEG_QUALITY, optimize=True)

        logger.debug(f"保存: {save_path} ({img.size[0]}x{img.size[1]}px)")
        return True

    except Exception as e:
        logger.error(f"画像処理エラー ({url}): {e}")
        return False


def download_product_images(mw_model_id: str, image_urls: list[str]) -> list[str]:
    """
    商品の画像URLリストをすべてダウンロードしてローカルパスのリストを返す
    既にダウンロード済みのファイルはスキップ

    Returns:
        成功したファイルの絶対パスリスト（最大9枚）
    """
    local_paths = []

    for i, url in enumerate(image_urls[:9]):  # Shopeeは最大9枚
        filename = f"{mw_model_id}_{i}.jpg"
        save_path = IMAGES_DIR / filename

        # 既にダウンロード済みならスキップ
        if save_path.exists() and save_path.stat().st_size > 1024:
            logger.debug(f"スキップ（既存）: {save_path}")
            local_paths.append(str(save_path))
            continue

        if download_image(url, save_path):
            local_paths.append(str(save_path))
        else:
            logger.warning(f"スキップ（ダウンロード失敗）: {url}")

        # リクエスト間にランダム待機
        if i < len(image_urls) - 1:
            time.sleep(random.uniform(0.5, 1.5))

    logger.info(f"画像ダウンロード完了: {len(local_paths)}/{len(image_urls[:9])} 枚 (model: {mw_model_id})")
    return local_paths


def get_cached_images(mw_model_id: str) -> list[str]:
    """
    既にダウンロード済みの画像パスを返す（ダウンロードは行わない）
    """
    paths = sorted(IMAGES_DIR.glob(f"{mw_model_id}_*.jpg"))
    return [str(p) for p in paths]


def cleanup_images(mw_model_id: str):
    """出品完了後に画像ファイルを削除してディスクを節約"""
    for path in IMAGES_DIR.glob(f"{mw_model_id}_*.jpg"):
        try:
            path.unlink()
            logger.debug(f"削除: {path}")
        except Exception as e:
            logger.warning(f"削除失敗: {path}: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # テスト
    test_urls = [
        "https://via.placeholder.com/800x800.jpg",
    ]
    paths = download_product_images("test_model_001", test_urls)
    print("ダウンロード済みパス:", paths)
