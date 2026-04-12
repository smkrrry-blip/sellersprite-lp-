"""
SQLiteデータベース管理
商品の進捗・重複防止・ログ管理
"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path

# マウント先のファイルシステムはSQLite非対応のためホームディレクトリに保存
# Mac上で実行する場合は ~/3d-shopee-bot/data/ に保存される
import os
_HOME = Path.home()
DB_DIR = _HOME / "3d-shopee-bot" / "data"
DB_PATH = DB_DIR / "products.db"


def get_conn():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """テーブル初期化"""
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        mw_model_id     TEXT UNIQUE NOT NULL,
        mw_url          TEXT,
        title_en        TEXT,
        title_th        TEXT,
        description_en  TEXT,
        description_th  TEXT,
        category        TEXT,
        tags            TEXT,
        image_urls      TEXT,       -- JSON array
        likes           INTEGER DEFAULT 0,
        makes           INTEGER DEFAULT 0,
        downloads       INTEGER DEFAULT 0,
        license         TEXT,
        commercial_ok   INTEGER DEFAULT 0,
        estimated_grams REAL DEFAULT 0,
        estimated_hours REAL DEFAULT 0,
        cost_thb        REAL DEFAULT 0,
        price_thb       REAL DEFAULT 0,
        status          TEXT DEFAULT 'scraped',
        -- status: scraped / translated / images_ready / listed / error
        shopee_item_id  TEXT,
        shopee_url      TEXT,
        created_at      TEXT,
        updated_at      TEXT,
        error_msg       TEXT
    );

    CREATE TABLE IF NOT EXISTS scrape_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at      TEXT,
        source      TEXT,
        total       INTEGER DEFAULT 0,
        new_items   INTEGER DEFAULT 0,
        listed      INTEGER DEFAULT 0,
        errors      INTEGER DEFAULT 0,
        notes       TEXT
    );

    CREATE TABLE IF NOT EXISTS shopee_images (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mw_model_id TEXT,
        original_url TEXT,
        shopee_image_id TEXT,
        created_at  TEXT
    );
    """)
    conn.commit()
    conn.close()
    print("✅ DB初期化完了")


def upsert_product(data: dict) -> bool:
    """商品を追加または更新。新規ならTrue、既存ならFalse"""
    conn = get_conn()
    now = datetime.now().isoformat()
    try:
        existing = conn.execute(
            "SELECT id FROM products WHERE mw_model_id=?", (data["mw_model_id"],)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE products SET
                    title_en=?, likes=?, makes=?, downloads=?,
                    commercial_ok=?, updated_at=?
                WHERE mw_model_id=?
            """, (
                data.get("title_en"), data.get("likes", 0),
                data.get("makes", 0), data.get("downloads", 0),
                data.get("commercial_ok", 0), now,
                data["mw_model_id"]
            ))
            conn.commit()
            return False
        else:
            conn.execute("""
                INSERT INTO products (
                    mw_model_id, mw_url, title_en, description_en,
                    category, tags, image_urls, likes, makes, downloads,
                    license, commercial_ok, status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                data["mw_model_id"], data.get("mw_url"), data.get("title_en"),
                data.get("description_en"), data.get("category"),
                json.dumps(data.get("tags", [])),
                json.dumps(data.get("image_urls", [])),
                data.get("likes", 0), data.get("makes", 0), data.get("downloads", 0),
                data.get("license"), data.get("commercial_ok", 0),
                "scraped", now, now
            ))
            conn.commit()
            return True
    finally:
        conn.close()


def update_status(mw_model_id: str, status: str, **kwargs):
    conn = get_conn()
    now = datetime.now().isoformat()
    sets = ["status=?", "updated_at=?"]
    vals = [status, now]
    for k, v in kwargs.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(mw_model_id)
    conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE mw_model_id=?", vals)
    conn.commit()
    conn.close()


def get_products_by_status(status: str, limit: int = 50) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM products WHERE status=? ORDER BY likes DESC LIMIT ?",
        (status, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_shopee_image(mw_model_id: str, original_url: str, shopee_image_id: str):
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO shopee_images
        (mw_model_id, original_url, shopee_image_id, created_at)
        VALUES (?,?,?,?)
    """, (mw_model_id, original_url, shopee_image_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_shopee_images(mw_model_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT shopee_image_id FROM shopee_images WHERE mw_model_id=?",
        (mw_model_id,)
    ).fetchall()
    conn.close()
    return [r["shopee_image_id"] for r in rows]


def log_run(source: str, total: int, new_items: int, listed: int, errors: int, notes=""):
    conn = get_conn()
    conn.execute("""
        INSERT INTO scrape_log (run_at, source, total, new_items, listed, errors, notes)
        VALUES (?,?,?,?,?,?,?)
    """, (datetime.now().isoformat(), source, total, new_items, listed, errors, notes))
    conn.commit()
    conn.close()


def get_stats() -> dict:
    conn = get_conn()
    stats = {}
    for status in ["scraped", "translated", "images_ready", "listed", "error"]:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM products WHERE status=?", (status,)
        ).fetchone()
        stats[status] = row["cnt"]
    stats["total"] = sum(stats.values())
    conn.close()
    return stats


if __name__ == "__main__":
    init_db()
    print(get_stats())
