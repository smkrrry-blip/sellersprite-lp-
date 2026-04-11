#!/bin/bash
# ─────────────────────────────────────────────────────────────
# 3D Shopee Bot — 初回セットアップスクリプト
# 実行方法: bash setup.sh
# ─────────────────────────────────────────────────────────────

set -e  # エラーで即終了

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(which python3)"
PIP="$(which pip3)"

echo "======================================"
echo "  3D Shopee Bot セットアップ開始"
echo "======================================"
echo "ディレクトリ: $BOT_DIR"
echo "Python: $PYTHON ($("$PYTHON" --version 2>&1))"
echo ""

# ── STEP 1: pip パッケージインストール ──────────────────────
echo "[1/4] pip パッケージインストール..."
"$PIP" install -r "$BOT_DIR/requirements.txt" --quiet
echo "  ✅ pip install 完了"

# ── STEP 2: Playwright インストール ─────────────────────────
echo "[2/4] Playwright Chromium インストール..."
"$PYTHON" -m playwright install chromium --quiet 2>&1 || {
    echo "  ⚠️  playwright install chromium に失敗しました"
    echo "     手動で実行: python3 -m playwright install chromium"
}
echo "  ✅ Playwright 完了"

# ── STEP 3: DB 初期化 ────────────────────────────────────────
echo "[3/4] データベース初期化..."
cd "$BOT_DIR" && "$PYTHON" -c "from db import init_db; init_db(); print('  ✅ DB 初期化完了')"

# ── STEP 4: ログ・データディレクトリ作成 ────────────────────
echo "[4/4] ディレクトリ作成..."
mkdir -p "$BOT_DIR/logs"
mkdir -p "$BOT_DIR/data"
chmod +x "$BOT_DIR/run_scheduler.sh"
echo "  ✅ logs/ data/ 作成完了"

# ── セットアップ完了 ─────────────────────────────────────────
echo ""
echo "======================================"
echo "  セットアップ完了！"
echo "======================================"
echo ""
echo "次のステップ:"
echo "  1. config.py に認証情報を入力:"
echo "     - SHOPEE_PARTNER_ID / SHOPEE_PARTNER_KEY"
echo "     - SHOPEE_SHOP_ID / SHOPEE_ACCESS_TOKEN"
echo "     - ANTHROPIC_API_KEY（翻訳用）"
echo ""
echo "  2. 接続テスト:"
echo "     python3 pipeline.py --step all --dry-run"
echo ""
echo "  3. cron 登録（毎日午前3時）:"
echo "     crontab -e"
echo "     → 0 3 * * * $BOT_DIR/run_scheduler.sh"
echo ""

osascript -e 'display notification "セットアップが完了しました" with title "3D Shopee Bot" sound name "Glass"' 2>/dev/null || true
