#!/bin/bash
# ─────────────────────────────────────────────────────────────
# 3D Shopee Bot — cron自動実行スクリプト
# cronに登録例（毎日午前3時に実行）:
#   0 3 * * * /Users/shoichionizuka/sellersprite-lp-/3d-shopee-bot/run_scheduler.sh
# ─────────────────────────────────────────────────────────────

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BOT_DIR/logs"
PYTHON="$(which python3)"
TIMESTAMP="$(date '+%Y-%m-%d_%H-%M-%S')"
LOG_FILE="$LOG_DIR/run_${TIMESTAMP}.log"
LATEST_LOG="$LOG_DIR/latest.log"

# ログディレクトリ作成
mkdir -p "$LOG_DIR"

echo "======================================" | tee -a "$LOG_FILE"
echo "開始: $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
echo "======================================" | tee -a "$LOG_FILE"

# パイプライン実行
cd "$BOT_DIR" && "$PYTHON" pipeline.py --step all 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

echo "======================================" | tee -a "$LOG_FILE"
echo "終了: $(date '+%Y-%m-%d %H:%M:%S') (exit=$EXIT_CODE)" | tee -a "$LOG_FILE"
echo "======================================" | tee -a "$LOG_FILE"

# latest.log にコピー（常に最新を確認できるように）
cp "$LOG_FILE" "$LATEST_LOG"

# 古いログを削除（30日以上前）
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete 2>/dev/null

# macOS 通知（成功/失敗に応じてメッセージを変える）
if [ "$EXIT_CODE" -eq 0 ]; then
    NOTIF_TITLE="✅ 3D Shopee Bot 完了"
    NOTIF_MSG="パイプライン正常終了。ログ: $LOG_FILE"
    SOUND="Glass"
else
    NOTIF_TITLE="❌ 3D Shopee Bot エラー"
    NOTIF_MSG="エラーが発生しました (exit=$EXIT_CODE)。ログを確認: $LOG_FILE"
    SOUND="Basso"
fi

osascript -e "display notification \"$NOTIF_MSG\" with title \"$NOTIF_TITLE\" sound name \"$SOUND\"" 2>/dev/null || true

exit $EXIT_CODE
