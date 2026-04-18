"""
毎朝6時（タイ時間）Shopee出品状況をTelegramへ送信
launchd: com.shoichi.shopee-daily-report.plist
"""
import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

BOT_TOKEN = "8772700188:AAFbw1U0tJFB56uX5z7veVAMQ-5dLbP1f6U"
CHAT_ID   = "7138196877"
BOT_DIR   = Path(__file__).parent
DB_PATH   = BOT_DIR / "data" / "products.db"  # db.py と同じパス
DAILY_COUNT_FILE = BOT_DIR / "data" / "daily_count.json"

# db.py のget_statsをそのまま使う
import sys
sys.path.insert(0, str(BOT_DIR))


def get_stats() -> dict:
    from db import get_stats as _db_stats
    s = _db_stats()

    # 最近7日間の出品数
    recent7 = 0
    today_count = 0
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            recent7 = conn.execute(
                "SELECT COUNT(*) FROM products WHERE status='listed'"
                " AND updated_at >= datetime('now','-7 days')"
            ).fetchone()[0]
            conn.close()
        except Exception:
            pass

    if DAILY_COUNT_FILE.exists():
        try:
            d = json.loads(DAILY_COUNT_FILE.read_text())
            if d.get("date") == str(datetime.now().date()):
                today_count = d.get("count", 0)
        except Exception:
            pass

    s["recent7"] = recent7
    s["today"]   = today_count
    return s


def send_telegram(text: str):
    subprocess.run([
        "curl", "-sS", "-X", "POST",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        "-d", f"chat_id={CHAT_ID}",
        "--data-urlencode", f"text={text}",
    ], capture_output=True)


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    s = get_stats()

    msg = (
        f"📦 Shopee 出品レポート\n"
        f"🕕 {now} (タイ時間)\n"
        f"━━━━━━━━━━━━━━\n"
        f"✅ 出品済み合計:  {s.get('listed', 0):>5} 件\n"
        f"📬 本日出品:      {s.get('today', 0):>5} 件\n"
        f"📅 直近7日:       {s.get('recent7', 0):>5} 件\n"
        f"━━━━━━━━━━━━━━\n"
        f"🖼 出品待ち:      {s.get('images_ready', 0):>5} 件\n"
        f"🌐 翻訳済み:      {s.get('translated', 0):>5} 件\n"
        f"📡 スクレイプ済:  {s.get('scraped', 0):>5} 件\n"
        f"❌ エラー:        {s.get('error', 0):>5} 件\n"
        f"━━━━━━━━━━━━━━\n"
        f"📊 DB総数:        {s.get('total', 0):>5} 件"
    )

    send_telegram(msg)
    print(msg)


if __name__ == "__main__":
    main()
