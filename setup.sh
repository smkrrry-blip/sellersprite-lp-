#!/bin/bash
# ================================================================
# setup.sh — セラースプライト LP ダッシュボード セットアップ
# 実行: bash ~/sellersprite-lp-/setup.sh
# ================================================================
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
KEY_DIR="$HOME/.config/sellersprite-dashboard"
KEY_FILE="$KEY_DIR/service_account.json"
PLIST_SRC="$REPO_DIR/com.sellersprite.dashboard.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.sellersprite.dashboard.plist"
SA_EMAIL="sellersprite-lp-dashboard@yahookeywordtool.iam.gserviceaccount.com"

echo "========================================================"
echo "  セラースプライト LP ダッシュボード セットアップ"
echo "========================================================"
echo ""

# ── STEP 1: Python ライブラリ確認 ──────────────────────────────
echo "[1/4] Pythonライブラリ確認..."
python3 -c "import google.oauth2.service_account" 2>/dev/null || {
  echo "  google-auth をインストール中..."
  pip3 install --quiet google-auth google-auth-httplib2 requests
}
python3 -c "import requests" 2>/dev/null || pip3 install --quiet requests
echo "      ✅ ライブラリOK"
echo ""

# ── STEP 2: サービスアカウントキー確認 ──────────────────────────
if [ ! -f "$KEY_FILE" ]; then
  echo "[2/4] ⚠️  サービスアカウントキーが見つかりません"
  echo ""
  echo "  配置先: $KEY_FILE"
  echo ""
  echo "  ▼ キー取得手順:"
  echo "  1. Cloud Consoleを開く:"
  echo "     https://console.cloud.google.com/iam-admin/serviceaccounts?project=yahookeywordtool"
  echo ""
  echo "  2. $SA_EMAIL"
  echo "     → 「キー」タブ → 「キーを追加」→「新しいキーを作成」→ JSON"
  echo ""
  echo "  3. ダウンロードしたJSONを配置:"
  echo "     mkdir -p $KEY_DIR"
  echo "     mv ~/Downloads/<ダウンロードしたファイル>.json $KEY_FILE"
  echo "     chmod 600 $KEY_FILE"
  echo ""
  echo "  ▼ GA4 権限追加（未設定の場合）:"
  echo "     https://analytics.google.com/analytics/web/"
  echo "     管理 → プロパティ(530190563) → プロパティアクセス管理 → ＋追加"
  echo "     メール: $SA_EMAIL  役割: 閲覧者"
  echo ""
  echo "  ▼ Search Console 権限追加（未設定の場合）:"
  echo "     https://search.google.com/search-console/"
  echo "     設定 → ユーザーと権限 → ユーザーを追加"
  echo "     メール: $SA_EMAIL  権限: 制限付き"
  echo ""
  read -rp "  キーを配置したら Enter を押してください..."
  echo ""
fi

if [ ! -f "$KEY_FILE" ]; then
  echo "  ❌ キーファイルが見つかりません: $KEY_FILE"
  echo "  上記手順でキーを配置してから再実行してください。"
  exit 1
fi
echo "[2/4] ✅ サービスアカウントキー確認済み"
echo ""

# ── STEP 3: launchd エージェント登録 ────────────────────────────
echo "[3/4] launchd エージェントをインストール中..."
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DEST"
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load -w "$PLIST_DEST"
echo "      ✅ インストール完了（毎朝7:00に自動実行）"
echo ""

# ── STEP 4: 初回データ取得 + git push ────────────────────────────
echo "[4/4] 初回データ取得中..."
python3 "$REPO_DIR/fetch_dashboard.py"
echo ""

echo "  data.json を GitHub Pages にデプロイ中..."
cd "$REPO_DIR"
git add data.json
git diff --cached --quiet || git commit -m "chore: initial dashboard data.json"
git push
echo ""

echo "========================================================"
echo "  ✅ セットアップ完了！"
echo ""
echo "  ダッシュボード: https://sellersprite.blog/dashboard.html"
echo ""
echo "  自動更新: 毎朝 07:00（launchd）"
echo "  手動更新: python3 $REPO_DIR/fetch_dashboard.py && cd $REPO_DIR && git add data.json && git commit -m 'chore: update dashboard data' && git push"
echo "  ログ確認: tail -f /tmp/sellersprite-dashboard.log"
echo "  エラー確認: cat /tmp/sellersprite-dashboard.err"
echo "========================================================"
