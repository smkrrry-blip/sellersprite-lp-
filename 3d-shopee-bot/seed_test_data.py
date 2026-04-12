"""
テスト用データをDBに直接投入するスクリプト
MakerWorldスクレイピングが動くまでの暫定措置
"""
from db import init_db, upsert_product
import json

TEST_PRODUCTS = [
    {
        "mw_model_id": "mw_test_001",
        "title_en": "Minimalist Phone Stand",
        "description_en": "A clean, minimalist phone stand perfect for desk use. Supports all phone sizes. Print time approximately 2 hours with PLA filament.",
        "category": "Home & Living",
        "image_urls": json.dumps([
            "https://images.unsplash.com/photo-1511707171634-5f897ff02aa9?w=800",
            "https://images.unsplash.com/photo-1556656793-08538906a9f8?w=800",
        ]),
        "source_url": "https://makerworld.com/en/models/test001",
        "license": "CC BY",
        "likes_count": 245,
        "makes_count": 89,
        "estimated_grams": 45,
        "estimated_hours": 2.0,
        "status": "scraped",
    },
    {
        "mw_model_id": "mw_test_002",
        "title_en": "Cable Management Clip Set",
        "description_en": "Set of 5 cable management clips for desk organization. Easy to install, no tools required. Compatible with desks up to 3cm thick.",
        "category": "Home & Living",
        "image_urls": json.dumps([
            "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=800",
        ]),
        "source_url": "https://makerworld.com/en/models/test002",
        "license": "CC BY",
        "likes_count": 187,
        "makes_count": 63,
        "estimated_grams": 30,
        "estimated_hours": 1.5,
        "status": "scraped",
    },
    {
        "mw_model_id": "mw_test_003",
        "title_en": "Succulent Planter Geometric",
        "description_en": "Modern geometric planter for small succulents and cacti. Available in multiple sizes. Print with PETG for water resistance.",
        "category": "Home & Living",
        "image_urls": json.dumps([
            "https://images.unsplash.com/photo-1416879595882-3373a0480b5b?w=800",
            "https://images.unsplash.com/photo-1485955900006-10f4d324d411?w=800",
        ]),
        "source_url": "https://makerworld.com/en/models/test003",
        "license": "CC0",
        "likes_count": 312,
        "makes_count": 120,
        "estimated_grams": 80,
        "estimated_hours": 3.5,
        "status": "scraped",
    },
]

if __name__ == "__main__":
    init_db()
    count = 0
    for product in TEST_PRODUCTS:
        is_new = upsert_product(product)
        status = "新規追加" if is_new else "既存（スキップ）"
        print(f"  {status}: {product['title_en']}")
        if is_new:
            count += 1
    print(f"\n✅ テストデータ投入完了: {count} 件追加")
    print("次のステップ: python3 pipeline.py --step translate")
