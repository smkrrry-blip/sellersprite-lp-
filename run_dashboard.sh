#!/bin/bash
# run_dashboard.sh — データ取得 + git push を一括実行
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DATE=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$LOG_DATE] 開始"

# データ取得
python3 "$REPO_DIR/fetch_dashboard.py"

# 変更があればcommit & push（data.json と KPI履歴CSV）
cd "$REPO_DIR"
if ! git diff --quiet data.json kpi_history.csv; then
    git add data.json kpi_history.csv
    git commit -m "chore: update dashboard data $(date '+%Y-%m-%d')"
    git push
    echo "[$LOG_DATE] push完了"
else
    echo "[$LOG_DATE] 変更なし、pushスキップ"
fi
