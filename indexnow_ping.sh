#!/bin/bash
# indexnow_ping.sh — 更新したページをBing(IndexNow)に即時通知する
# 使い方:
#   bash indexnow_ping.sh                      → 刈り取り8ページ+トップを通知
#   bash indexnow_ping.sh /ryoukin.html ...    → 指定ページだけ通知
set -e

KEY="8f76b50100d8e8d33779e3c060bc211e"
HOST="sellersprite.blog"

if [ $# -gt 0 ]; then
    PATHS=("$@")
else
    PATHS=(/ /index.html /coupon-cj9852.html /waribiki.html /hyouban.html /ryoukin.html /tsukaikata.html /touroku.html /vs-helium10.html)
fi

URL_LIST=""
for p in "${PATHS[@]}"; do
    URL_LIST="${URL_LIST}\"https://${HOST}${p}\","
done
URL_LIST="${URL_LIST%,}"

RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://api.indexnow.org/indexnow" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d "{\"host\":\"${HOST}\",\"key\":\"${KEY}\",\"keyLocation\":\"https://${HOST}/${KEY}.txt\",\"urlList\":[${URL_LIST}]}")

echo "IndexNow response: HTTP ${RESPONSE} (200/202=成功)"
