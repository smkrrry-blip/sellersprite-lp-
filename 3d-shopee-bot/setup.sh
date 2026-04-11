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
echo "  （Playwright ブラウザ自動化版）"
echo "======================================"
echo "ディレクトリ: $BOT_DIR"
echo "Python: $PYTHON ($("$PYTHON" --version 2>&1))"
echo ""

# ── STEP 1: pip パッケージインストール ──────────────────────
echo "[1/5] pip パッケージインストール..."
"$PIP" install -r "$BOT_DIR/requirements.txt" --quiet
echo "  ✅ pip install 完了"

# ── STEP 2: Playwright Chromium インストール ─────────────────
echo "[2/5] Playwright Chromium インストール..."
"$PYTHON" -m playwright install chromium 2>&1 || {
    echo "  ⚠️  playwright install chromium に失敗しました"
    echo "     手動で実行: python3 -m playwright install chromium"
    exit 1
}
echo "  ✅ Playwright Chromium インストール完了"

# ── STEP 3: DB 初期化 ────────────────────────────────────────
echo "[3/5] データベース初期化..."
cd "$BOT_DIR" && "$PYTHON" -c "from db import init_db; init_db(); print('  ✅ DB 初期化完了')"

# ── STEP 4: ディレクトリ作成 ─────────────────────────────────
echo "[4/5] ディレクトリ作成..."
mkdir -p "$BOT_DIR/logs"
mkdir -p "$BOT_DIR/data"
mkdir -p "$BOT_DIR/images"
mkdir -p "$BOT_DIR/errors"
chmod +x "$BOT_DIR/run_scheduler.sh"
echo "  ✅ logs/ data/ images/ errors/ 作成完了"

# ── STEP 5: 設定ファイル確認 ─────────────────────────────────
echo "[5/5] config.py の設定確認..."
MISSING=0
if "$PYTHON" -c "from config import SHOPEE_EMAIL; assert SHOPEE_EMAIL, 'empty'" 2>/dev/null; then
    echo "  ✅ SHOPEE_EMAIL 設定済み"
else
    echo "  ⚠️  SHOPEE_EMAIL が未設定です"
    MISSING=1
fi
if "$PYTHON" -c "from config import SHOPEE_PASSWORD; assert SHOPEE_PASSWORD, 'empty'" 2>/dev/null; then
    echo "  ✅ SHOPEE_PASSWORD 設定済み"
else
    echo "  ⚠️  SHOPEE_PASSWORD が未設定です"
    MISSING=1
fi

# ── セットアップ完了 ─────────────────────────────────────────
echo ""
echo "======================================"
echo "  セットアップ完了！"
echo "======================================"
echo ""
echo "次のステップ:"
echo "  1. config.py に認証情報を入力:"
echo "     - SHOPEE_EMAIL    … セラーセンターのメールアドレス"
echo "     - SHOPEE_PASSWORD … セラーセンターのパスワード"
echo "     - ANTHROPIC_API_KEY（翻訳用・任意）"
echo ""
echo "  2. ドライランでテスト:"
echo "     python3 pipeline.py --step all --dry-run"
echo ""
echo "  3. ブラウザでログインのみ確認:"
echo "     python3 shopee_browser.py"
echo ""
echo "  4. 本番実行:"
echo "     python3 pipeline.py --step all"
echo ""
echo "  5. cron 登録（毎日午前3時）:"
echo "     crontab -e"
echo "     → 0 3 * * * $BOT_DIR/run_scheduler.sh"
echo ""

if [ "$MISSING" -eq 1 ]; then
    osascript -e 'display notification "config.py に認証情報を入力してください" with title "3D Shopee Bot" subtitle "セットアップ未完了" sound name "Sosumi"' 2>/dev/null || true
else
    osascript -e 'display notification "セットアップが完了しました" with title "3D Shopee Bot" sound name "Glass"' 2>/dev/null || true
fi
