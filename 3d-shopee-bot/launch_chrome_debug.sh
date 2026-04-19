#!/usr/bin/env bash
# Chrome をリモートデバッグモード (CDP) で起動する
# 実行後: Shopee Seller Centre にログイン → shopee-run --live で出品
#
# 使い方:
#   ./launch_chrome_debug.sh          # CDP ポート 9222 で起動
#   ./launch_chrome_debug.sh 9223     # ポート指定

set -euo pipefail

PORT="${1:-9222}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# 専用プロファイル（通常のChromeと干渉しない）
PROFILE_DIR="$HOME/.shopee-bot-chrome-profile"

if ! [[ -f "$CHROME" ]]; then
  echo "❌ Chrome が見つかりません: $CHROME"
  exit 1
fi

# すでに CDP ポートが使われているか確認
if lsof -i ":$PORT" &>/dev/null; then
  echo "⚠️  ポート $PORT は既に使用中です（Chromeが起動済みの可能性）"
  echo "   既存 Chrome をそのまま使う場合は shopee-run --live を実行してください"
  exit 0
fi

echo "🚀 Chrome をデバッグモードで起動 (port $PORT)..."
echo "   プロファイル: $PROFILE_DIR"
echo ""
echo "  1. 起動後に Shopee Seller Centre が開きます"
echo "  2. ログインしてください"
echo "  3. 別ターミナルで: shopee-run --live"
echo ""

mkdir -p "$PROFILE_DIR"

"$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  "https://seller.shopee.co.th/portal/product/list/all" \
  &

sleep 3
echo "✅ Chrome 起動完了 (PID $!)"
echo "   CDP: http://localhost:$PORT"
