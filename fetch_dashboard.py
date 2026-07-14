#!/usr/bin/env python3
"""
fetch_dashboard.py  – GA4 + Search Console データ取得 → data.json 書き出し
サービスアカウントキー (~/.config/sellersprite-dashboard/service_account.json) を使用。
"""

import json
import os
import sys
import datetime
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── 設定 ──────────────────────────────────────────────────────────────────────
KEY_FILE     = os.path.expanduser('~/.config/sellersprite-dashboard/service_account.json')
USER_TOKEN   = os.path.expanduser('~/.config/sellersprite-dashboard/user_token.json')
OAUTH_CLIENT = os.path.expanduser('~/.config/sellersprite-dashboard/oauth_client.json')
GA4_PROPERTY = '530190563'
GSC_SITE     = 'sc-domain:sellersprite.blog'
OUTPUT_FILE  = os.path.join(os.path.dirname(__file__), 'data.json')
DAYS         = 30   # 直近 N 日分を取得

SA_SCOPES = [
    'https://www.googleapis.com/auth/analytics.readonly',
]

# ── SA 認証 (GA4用) ───────────────────────────────────────────────────────────
def get_sa_token():
    if not os.path.exists(KEY_FILE):
        print(f'[ERROR] キーファイルが見つかりません: {KEY_FILE}', file=sys.stderr)
        sys.exit(1)
    creds = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=SA_SCOPES)
    creds.refresh(Request())
    return creds.token

# ── User OAuth 認証 (GSC用) ───────────────────────────────────────────────────
def get_user_token():
    if not os.path.exists(USER_TOKEN):
        print('[WARN] user_token.json が見つかりません。GSCデータをスキップします。', file=sys.stderr)
        return None
    tok = json.load(open(USER_TOKEN))
    if 'refresh_token' not in tok:
        return tok.get('access_token')
    # リフレッシュ
    cs = json.load(open(OAUTH_CLIENT))['installed']
    r = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': cs['client_id'],
        'client_secret': cs['client_secret'],
        'refresh_token': tok['refresh_token'],
        'grant_type': 'refresh_token',
    })
    if r.status_code == 200:
        new_tok = r.json()
        tok['access_token'] = new_tok['access_token']
        json.dump(tok, open(USER_TOKEN, 'w'))
        return tok['access_token']
    print(f'[WARN] トークンリフレッシュ失敗: {r.text[:100]}', file=sys.stderr)
    return None  # 失効した古いトークンを返さない（ゼロデータの静かな記録を防ぐ）

# ── 後方互換 ──────────────────────────────────────────────────────────────────
def get_token():
    return get_sa_token()

# ── 日付 ──────────────────────────────────────────────────────────────────────
def date_range(days):
    end   = datetime.date.today()
    start = end - datetime.timedelta(days=days - 1)
    return str(start), str(end)

# ── GA4 Data API ──────────────────────────────────────────────────────────────
def fetch_ga4(token, start_date, end_date):
    url = f'https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY}:runReport'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    def run_report(body):
        r = requests.post(url, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    # ページ別 PV / セッション / ユーザー
    pv_body = {
        'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
        'dimensions': [{'name': 'pagePath'}],
        'metrics': [
            {'name': 'screenPageViews'},
            {'name': 'sessions'},
            {'name': 'activeUsers'},
        ],
        'limit': 2000,
    }
    pv_data = run_report(pv_body)

    # ページ別 code_copy イベント数
    copy_body = {
        'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
        'dimensions': [{'name': 'pagePath'}],
        'metrics': [{'name': 'eventCount'}],
        'dimensionFilter': {
            'filter': {'fieldName': 'eventName', 'stringFilter': {'value': 'code_copy'}}
        },
        'limit': 2000,
    }
    copy_data = run_report(copy_body)

    copy_map = {}
    for row in copy_data.get('rows', []):
        path = row['dimensionValues'][0]['value']
        copy_map[path] = int(row['metricValues'][0]['value'] or 0)

    result = {}
    for row in pv_data.get('rows', []):
        path = row['dimensionValues'][0]['value']
        result[path] = {
            'pv':       int(row['metricValues'][0]['value'] or 0),
            'sessions': int(row['metricValues'][1]['value'] or 0),
            'users':    int(row['metricValues'][2]['value'] or 0),
            'copies':   copy_map.get(path, 0),
        }
    return result

# ── Search Console API ────────────────────────────────────────────────────────
def fetch_gsc(token, start_date, end_date):
    site = requests.utils.quote(GSC_SITE, safe='')
    url  = f'https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    body = {
        'startDate':  start_date,
        'endDate':    end_date,
        'dimensions': ['page'],
        'rowLimit':   2000,
    }
    r = requests.post(url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()

    base = 'https://sellersprite.blog'
    result = {}
    for row in data.get('rows', []):
        full_url = row['keys'][0]
        path = full_url.replace(base, '') or '/'
        result[path] = {
            'impressions': row.get('impressions', 0),
            'clicks':      row.get('clicks', 0),
            'ctr':         row.get('ctr', 0),
            'position':    row.get('position', 0),
        }
    return result

# ── マージ & 書き出し ─────────────────────────────────────────────────────────
def main():
    start_date, end_date = date_range(DAYS)
    print(f'[INFO] 取得期間: {start_date} 〜 {end_date}')

    token = get_token()
    print('[INFO] SA認証完了')

    user_token = get_user_token()
    print(f'[INFO] ユーザートークン: {"取得済" if user_token else "なし（GSCスキップ）"}')

    print('[INFO] GA4 取得中...')
    try:
        ga4 = fetch_ga4(token, start_date, end_date)
        print(f'[INFO] GA4: {len(ga4)} ページ')
    except Exception as e:
        print(f'[WARN] GA4 取得失敗: {e}', file=sys.stderr)
        ga4 = {}

    print('[INFO] Search Console 取得中...')
    gsc_status = 'ok'
    try:
        if user_token:
            gsc = fetch_gsc(user_token, start_date, end_date)
            print(f'[INFO] GSC: {len(gsc)} ページ')
        else:
            print('[WARN] GSCトークンなし（認証失効の可能性）。GSCデータをスキップ。', file=sys.stderr)
            gsc = {}
            gsc_status = 'error'
    except Exception as e:
        print(f'[WARN] GSC 取得失敗: {e}', file=sys.stderr)
        gsc = {}
        gsc_status = 'error'

    all_paths = sorted(set(list(ga4.keys()) + list(gsc.keys())))
    rows = []
    for path in all_paths:
        g = ga4.get(path, {'pv': 0, 'sessions': 0, 'users': 0, 'copies': 0})
        s = gsc.get(path, {'impressions': 0, 'clicks': 0, 'ctr': 0, 'position': 0})
        cvr = g['copies'] / g['sessions'] if g['sessions'] > 0 else 0
        rows.append({
            'path':        path,
            'pv':          g['pv'],
            'sessions':    g['sessions'],
            'users':       g['users'],
            'copies':      g['copies'],
            'cvr':         round(cvr, 6),
            'impressions': s['impressions'],
            'clicks':      s['clicks'],
            'ctr':         round(s['ctr'], 6),
            'position':    round(s['position'], 2),
        })

    # 総計カード用
    total_pv  = sum(r['pv']  for r in rows)
    total_imp = sum(r['impressions'] for r in rows)
    total_clk = sum(r['clicks']  for r in rows)
    total_ses = sum(r['sessions'] for r in rows)
    total_cop = sum(r['copies']  for r in rows)

    output = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'gsc_status':   gsc_status,
        'date_range':   {'start': start_date, 'end': end_date},
        'summary': {
            'total_pv':          total_pv,
            'total_impressions': total_imp,
            'total_clicks':      total_clk,
            'avg_ctr':           round(total_clk / total_imp, 6) if total_imp else 0,
            'avg_cvr':           round(total_cop / total_ses, 6) if total_ses else 0,
        },
        'rows': rows,
    }

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f'[INFO] 書き出し完了: {OUTPUT_FILE} ({len(rows)} ページ)')

if __name__ == '__main__':
    main()
