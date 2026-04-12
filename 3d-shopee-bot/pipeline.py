"""
メインパイプライン
MakerWorld → 翻訳 → 画像DL → Shopeeブラウザ出品 を一括実行
"""
import json
import logging
import traceback
from datetime import datetime


def _parse_image_urls(raw) -> list[str]:
    """
    image_urls フィールドを確実にリスト化する
    DBが二重エンコードする場合があるため、json.loads を最大2回実施
    """
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
        if isinstance(result, str):
            result2 = json.loads(result)
            if isinstance(result2, list):
                return result2
    except Exception:
        pass
    return []
from db import init_db, upsert_product, update_status, get_products_by_status, get_stats, log_run
from makerworld import MakerWorldScraper
from translator import Translator, PriceCalculator
from shopee_browser import ShopeeBrowser, _get_today_count
from image_downloader import download_product_images, cleanup_images
from config import SCRAPING_SETTINGS, BROWSER_SETTINGS

logger = logging.getLogger(__name__)


def run_full_pipeline(dry_run: bool = False):
    """
    完全パイプライン実行
    1. MakerWorldスクレイピング
    2. DB保存
    3. 翻訳
    4. 画像ダウンロード（ローカル保存）
    5. Shopeeブラウザ出品

    dry_run=True: 実際には出品せず、データ取得・翻訳・画像DLのみ行う
    """
    logger.info("=" * 60)
    logger.info(f"🚀 パイプライン開始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"   モード: {'DRY RUN (出品なし)' if dry_run else '本番出品（ブラウザ自動化）'}")
    logger.info("=" * 60)

    init_db()
    stats_start = get_stats()
    logger.info(f"DB状態: {stats_start}")

    # 1日の出品上限確認
    if not dry_run:
        today_count = _get_today_count()
        remaining = BROWSER_SETTINGS["daily_limit"] - today_count
        if remaining <= 0:
            logger.warning(f"⚠️ 本日の出品上限 ({BROWSER_SETTINGS['daily_limit']}件) に達しています。終了します。")
            return {"listed": 0, "errors": 0}
        logger.info(f"本日の出品可能残数: {remaining} 件")

    scraper    = MakerWorldScraper()
    translator = Translator()
    counters   = {"total": 0, "new": 0, "translated": 0, "images": 0, "listed": 0, "errors": 0}

    # ─── STEP 1: MakerWorldスクレイピング ───────────────
    logger.info("\n📡 STEP 1: MakerWorldスクレイピング")
    scraper.login()

    models = scraper.get_trending_models(limit=SCRAPING_SETTINGS["items_per_run"])
    counters["total"] = len(models)
    logger.info(f"  取得: {len(models)} 件")

    for model in models:
        is_new = upsert_product(model)
        if is_new:
            counters["new"] += 1
    logger.info(f"  新規: {counters['new']} 件")

    # ─── STEP 2: 翻訳（未翻訳のもの）───────────────────
    logger.info("\n🌐 STEP 2: 翻訳")
    to_translate = get_products_by_status("scraped", limit=SCRAPING_SETTINGS["items_per_run"])
    logger.info(f"  翻訳対象: {len(to_translate)} 件")

    for product in to_translate:
        try:
            if not product.get("description_en"):
                detail = scraper.get_model_detail(product["mw_model_id"])
                if detail:
                    product.update(detail)

            translated = translator.translate_product(product)
            price_data = PriceCalculator.calculate(
                weight_g=product.get("estimated_grams") or 50,
                print_hours=product.get("estimated_hours") or 3,
            )
            update_status(
                product["mw_model_id"], "translated",
                title_th=translated.get("title_th"),
                description_th=translated.get("description_th"),
                price_thb=price_data["price_thb"],
                cost_thb=price_data["cost_thb"],
            )
            counters["translated"] += 1
            logger.info(f"  ✅ 翻訳完了: {translated.get('title_th', '')[:40]}")

        except Exception as e:
            logger.error(f"  ❌ 翻訳エラー ({product['mw_model_id']}): {e}")
            update_status(product["mw_model_id"], "error", error_msg=str(e))
            counters["errors"] += 1

    # ─── STEP 3: 画像ダウンロード ────────────────────────
    logger.info("\n🖼️ STEP 3: 画像ダウンロード（ローカル保存）")
    to_download = get_products_by_status("translated", limit=SCRAPING_SETTINGS["items_per_run"])
    logger.info(f"  ダウンロード対象: {len(to_download)} 件")

    for product in to_download:
        try:
            image_urls = _parse_image_urls(product.get("image_urls"))
            if not image_urls:
                logger.warning(f"  ⚠️ 画像URLなし: {product['mw_model_id']}")
                update_status(product["mw_model_id"], "error", error_msg="no image urls")
                continue

            if dry_run:
                logger.info(f"  [DRY] 画像URL確認: {len(image_urls)} 枚")
                update_status(product["mw_model_id"], "images_ready")
                continue

            local_paths = download_product_images(product["mw_model_id"], image_urls)
            if local_paths:
                update_status(product["mw_model_id"], "images_ready")
                counters["images"] += 1
                logger.info(f"  ✅ 画像DL: {len(local_paths)} 枚 / {product.get('title_en', '')[:30]}")
            else:
                update_status(product["mw_model_id"], "error", error_msg="image download failed")
                counters["errors"] += 1

        except Exception as e:
            logger.error(f"  ❌ 画像エラー ({product['mw_model_id']}): {e}")
            counters["errors"] += 1

    # ─── STEP 4: Shopeeブラウザ出品 ──────────────────────
    logger.info("\n🛒 STEP 4: Shopeeブラウザ出品")
    to_list = get_products_by_status("images_ready", limit=BROWSER_SETTINGS["daily_limit"])
    logger.info(f"  出品対象: {len(to_list)} 件")

    if not dry_run and to_list:
        with ShopeeBrowser() as browser:
            if not browser.login():
                logger.error("  ❌ Shopeeログイン失敗。出品をスキップします。")
            else:
                for product in to_list:
                    # 上限再チェック（ループ内で更新されるため）
                    if _get_today_count() >= BROWSER_SETTINGS["daily_limit"]:
                        logger.warning("  ⚠️ 1日の出品上限に達しました")
                        break

                    try:
                        from image_downloader import get_cached_images
                        local_paths = get_cached_images(product["mw_model_id"])
                        if not local_paths:
                            image_urls = _parse_image_urls(product.get("image_urls"))
                            local_paths = download_product_images(product["mw_model_id"], image_urls)

                        if not local_paths:
                            logger.warning(f"  ⚠️ 画像なし: {product['mw_model_id']}")
                            continue

                        shopee_url = browser.list_product(product, local_paths)
                        if shopee_url:
                            update_status(
                                product["mw_model_id"], "listed",
                                shopee_url=shopee_url,
                            )
                            counters["listed"] += 1
                            logger.info(f"  ✅ 出品: {product.get('title_th', '')[:40]}")
                            # 出品完了後に画像を削除
                            cleanup_images(product["mw_model_id"])
                        else:
                            update_status(product["mw_model_id"], "error", error_msg="browser listing failed")
                            counters["errors"] += 1

                    except Exception as e:
                        logger.error(f"  ❌ 出品エラー ({product['mw_model_id']}): {traceback.format_exc()}")
                        counters["errors"] += 1
    elif dry_run:
        for product in to_list:
            logger.info(f"  [DRY] 出品スキップ: {product.get('title_th', '')[:40]}")
            logger.info(f"        価格: {product.get('price_thb')} THB")

    # ─── サマリー ────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("📊 実行サマリー")
    logger.info(f"  スクレイプ: {counters['total']} 件 (新規: {counters['new']} 件)")
    logger.info(f"  翻訳完了:  {counters['translated']} 件")
    logger.info(f"  画像DL:    {counters['images']} 件")
    logger.info(f"  出品完了:  {counters['listed']} 件")
    logger.info(f"  エラー:    {counters['errors']} 件")
    logger.info("=" * 60)

    log_run(
        source="MakerWorld",
        total=counters["total"],
        new_items=counters["new"],
        listed=counters["listed"],
        errors=counters["errors"],
        notes="dry_run" if dry_run else "browser_automation",
    )

    return counters


def run_step(step: str):
    """個別ステップのみ実行"""
    init_db()
    if step == "scrape":
        scraper = MakerWorldScraper()
        scraper.login()
        models = scraper.get_trending_models(limit=50)
        new = sum(1 for m in models if upsert_product(m))
        print(f"✅ スクレイプ完了: {len(models)} 件取得 / {new} 件新規")

    elif step == "translate":
        translator = Translator()
        products = get_products_by_status("scraped", limit=50)
        for p in products:
            translated = translator.translate_product(p)
            price = PriceCalculator.calculate()
            update_status(p["mw_model_id"], "translated",
                          title_th=translated.get("title_th"),
                          description_th=translated.get("description_th"),
                          price_thb=price["price_thb"])
        print(f"✅ 翻訳完了: {len(products)} 件")

    elif step == "download":
        products = get_products_by_status("translated", limit=20)
        for p in products:
            image_urls = _parse_image_urls(p.get("image_urls"))
            paths = download_product_images(p["mw_model_id"], image_urls)
            if paths:
                update_status(p["mw_model_id"], "images_ready")
        print(f"✅ 画像ダウンロード完了: {len(products)} 件")

    elif step == "list":
        products = get_products_by_status("images_ready", limit=BROWSER_SETTINGS["daily_limit"])
        listed = 0
        with ShopeeBrowser() as browser:
            if browser.login():
                for p in products:
                    if _get_today_count() >= BROWSER_SETTINGS["daily_limit"]:
                        break
                    local_paths = get_cached_images(p["mw_model_id"])
                    if not local_paths:
                        image_urls = _parse_image_urls(p.get("image_urls"))
                        local_paths = download_product_images(p["mw_model_id"], image_urls)
                    if local_paths:
                        url = browser.list_product(p, local_paths)
                        if url:
                            update_status(p["mw_model_id"], "listed", shopee_url=url)
                            cleanup_images(p["mw_model_id"])
                            listed += 1
        print(f"✅ 出品完了: {listed} 件")

    elif step == "status":
        stats = get_stats()
        print("📊 DB状態:")
        for k, v in stats.items():
            print(f"  {k}: {v} 件")
        today = _get_today_count()
        print(f"  本日の出品数: {today}/{BROWSER_SETTINGS['daily_limit']} 件")


if __name__ == "__main__":
    import sys
    import argparse
    from image_downloader import get_cached_images

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="MakerWorld→Shopee 自動出品パイプライン（ブラウザ自動化）")
    parser.add_argument(
        "--step",
        choices=["scrape", "translate", "download", "list", "status", "all"],
        default="all",
        help="実行ステップ（download: 画像ダウンロード）",
    )
    parser.add_argument("--dry-run", action="store_true", help="出品せずにテスト実行")
    args = parser.parse_args()

    if args.step == "all":
        run_full_pipeline(dry_run=args.dry_run)
    else:
        run_step(args.step)
