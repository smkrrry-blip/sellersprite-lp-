#!/bin/bash
# run_dashboard.sh — データ取得 + git push を一括実行
#
# 2026-09-07 改訂：失敗を黙って飲み込まないようにした。
#   従来は set -e で途中終了するだけで、ログを見に行かない限り誰も気づけなかった。
#   実際 2026-09-02〜06 の5日間、計測が止まっていることに誰も気づかなかった。

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DATE=$(date '+%Y-%m-%d %H:%M:%S')
TODAY=$(date '+%Y-%m-%d')

echo "[$LOG_DATE] 開始"

# launchd は ~/.zshrc を読まないため、メール送信用のパスワードを明示的に取り出す
if [ -z "${GMAIL_APP_PASSWORD:-}" ] && [ -f "$HOME/.zshrc" ]; then
    export GMAIL_APP_PASSWORD="$(sed -n 's/^[[:space:]]*export[[:space:]]*GMAIL_APP_PASSWORD=["'"'"']\{0,1\}\([^"'"'"']*\).*/\1/p' "$HOME/.zshrc" | tail -1)"
fi

# 失敗を鬼塚さんに届ける（メール＋デスクトップ通知）。通知自体が失敗しても本体は止めない。
notify_failure() {
    echo "[$LOG_DATE] 失敗: $1" >&2
    REPORT_SUBJECT='🚨【計測停止】sellersprite ' \
      /usr/local/bin/python3 "$REPO_DIR/send_report.py" \
      "ダッシュボードの自動更新に失敗しました。

原因: $1

確認: tail -40 $REPO_DIR/logs/dashboard.err
手動実行: bash $REPO_DIR/run_dashboard.sh" 2>/dev/null \
      || echo "[$LOG_DATE] 通知の送信にも失敗した" >&2
}

# データ取得（launchdと同じrequests入りのpythonを明示）
if ! /usr/local/bin/python3 "$REPO_DIR/fetch_dashboard.py"; then
    notify_failure "fetch_dashboard.py が異常終了した"
    exit 1
fi

# 当日行がCSVに入ったかを検証する。取得が「成功」しても行が増えていなければ計測は死んでいる。
if ! tail -5 "$REPO_DIR/kpi_history.csv" | grep -q "^$TODAY,"; then
    notify_failure "kpi_history.csv に本日（$TODAY）の行がない"
    exit 1
fi

# 変更があればcommit & push（data.json と KPI履歴CSV のみ。git add . は絶対に使わない）
cd "$REPO_DIR" || exit 1
if ! git diff --quiet data.json kpi_history.csv; then
    git add data.json kpi_history.csv
    git commit -m "chore: update dashboard data $(date '+%Y-%m-%d')"
    git push
    echo "[$LOG_DATE] push完了"
else
    echo "[$LOG_DATE] 変更なし、pushスキップ"
fi

echo "[$LOG_DATE] 正常終了"
