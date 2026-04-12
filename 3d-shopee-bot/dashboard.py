"""
ダッシュボード — ターミナルで進捗を表示
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from db import get_stats, DB_PATH


def show_dashboard():
    stats = get_stats()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    print("\n" + "═" * 55)
    print(f"  📊  3D Shopee Bot ダッシュボード  {now}")
    print("═" * 55)

    total = stats.get("total", 0)
    print(f"\n  📦 総商品数:    {total:>6} 件")
    print(f"  ⏳ スクレイプ済: {stats.get('scraped', 0):>6} 件")
    print(f"  🌐 翻訳済:      {stats.get('translated', 0):>6} 件")
    print(f"  🖼️  画像準備済:  {stats.get('images_ready', 0):>6} 件")
    print(f"  ✅ 出品済:      {stats.get('listed', 0):>6} 件")
    print(f"  ❌ エラー:      {stats.get('error', 0):>6} 件")

    # 最近の出品
    if Path(DB_PATH).exists():
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        recent = conn.execute("""
            SELECT title_th, price_thb, shopee_url, updated_at
            FROM products WHERE status='listed'
            ORDER BY updated_at DESC LIMIT 5
        """).fetchall()

        if recent:
            print("\n  📋 最近の出品:")
            for r in recent:
                title = (r["title_th"] or "")[:30]
                price = r["price_thb"] or 0
                dt = (r["updated_at"] or "")[:16]
                print(f"    [{dt}] {title:<32} {price:>6.0f} THB")

        # エラーログ
        errors = conn.execute("""
            SELECT title_en, error_msg, updated_at
            FROM products WHERE status='error'
            ORDER BY updated_at DESC LIMIT 3
        """).fetchall()
        if errors:
            print("\n  ⚠️  最近のエラー:")
            for e in errors:
                title = (e["title_en"] or "")[:30]
                err = (e["error_msg"] or "")[:40]
                print(f"    {title}: {err}")

        # 実行ログ
        logs = conn.execute("""
            SELECT run_at, total, new_items, listed, errors
            FROM scrape_log ORDER BY run_at DESC LIMIT 3
        """).fetchall()
        if logs:
            print("\n  📅 実行履歴:")
            for l in logs:
                dt = (l["run_at"] or "")[:16]
                print(f"    [{dt}] 取得:{l['total']} 新:{l['new_items']} 出品:{l['listed']} エラー:{l['errors']}")

        conn.close()

    print("\n" + "═" * 55)

    # 次のアクション
    next_steps = []
    if stats.get("scraped", 0) > 0:
        next_steps.append(f"  → python pipeline.py --step translate  ({stats['scraped']} 件待機中)")
    if stats.get("translated", 0) > 0:
        next_steps.append(f"  → python pipeline.py --step upload     ({stats['translated']} 件待機中)")
    if stats.get("images_ready", 0) > 0:
        next_steps.append(f"  → python pipeline.py --step list       ({stats['images_ready']} 件待機中)")
    if not next_steps:
        next_steps.append("  → python pipeline.py (全ステップ実行)")

    print("\n  🎯 次のコマンド:")
    for s in next_steps:
        print(s)
    print()


def show_listed_products(limit: int = 20):
    """出品済み商品一覧"""
    if not Path(DB_PATH).exists():
        print("DBが存在しません。まず pipeline.py を実行してください。")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT mw_model_id, title_th, price_thb, shopee_item_id, shopee_url, updated_at
        FROM products WHERE status='listed'
        ORDER BY updated_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    if not rows:
        print("出品済み商品はありません。")
        return

    print(f"\n出品済み商品一覧 ({len(rows)} 件):")
    print("-" * 80)
    for r in rows:
        print(f"ID: {r['mw_model_id']:<15} | {(r['title_th'] or '')[:35]:<35} | {r['price_thb']:>6.0f} THB")
        if r["shopee_url"]:
            print(f"  URL: {r['shopee_url']}")
    print("-" * 80)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        show_listed_products()
    else:
        show_dashboard()
