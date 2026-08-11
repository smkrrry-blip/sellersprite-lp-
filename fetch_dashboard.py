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

# AI検索・チャットボット経由の流入を検知するための参照元キーワード（導線B・LLMO計測）
# ドメインを丸ごと書くと copilot.com / openai(単体) のような表記ゆれを取りこぼすため、語幹で照合する
AI_SOURCE_KEYWORDS = ['chatgpt', 'openai', 'perplexity', 'gemini',
                      'claude.ai', 'copilot', 'you.com', 'phind', 'poe.com']

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

# ── イベントのページ別集計 ────────────────────────────────────────────────────
def event_page_map(run_report, start_date, end_date, event_name, jp_only=False):
    """指定イベントのページ別件数を返す。jp_only=True で日本からのアクセスに限定する。

    サイトの母数が小さい（30日で約190PV）ため、海外botが1体CTAを連打するだけで
    KPI6・7が2〜3倍に跳ねてしまう。実際 2026-08-10 に、30日15件のうち10件（67%）が
    米国発のbotだったことが判明した。実数は日本限定の集計で見る。
    """
    expr = [{'filter': {'fieldName': 'eventName', 'stringFilter': {'value': event_name}}}]
    if jp_only:
        expr.append({'filter': {'fieldName': 'countryId', 'stringFilter': {'value': 'JP'}}})
    body = {
        'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
        'dimensions': [{'name': 'pagePath'}],
        'metrics': [{'name': 'eventCount'}],
        'dimensionFilter': expr[0] if len(expr) == 1 else {'andGroup': {'expressions': expr}},
        'limit': 2000,
    }
    return {r['dimensionValues'][0]['value']: int(r['metricValues'][0]['value'] or 0)
            for r in run_report(body).get('rows', [])}

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

    # ページ別 code_copy / cta_click（登録ボタンのクリック）
    # _jp 付きは日本からのアクセスのみ＝bot汚染を除いた実数。KPI6・7の正はこちら
    em = lambda name, jp: event_page_map(run_report, start_date, end_date, name, jp)
    copy_map    = em('code_copy', False)
    cta_map     = em('cta_click', False)
    copy_map_jp = em('code_copy', True)
    cta_map_jp  = em('cta_click', True)

    result = {}
    for row in pv_data.get('rows', []):
        path = row['dimensionValues'][0]['value']
        result[path] = {
            'pv':       int(row['metricValues'][0]['value'] or 0),
            'sessions': int(row['metricValues'][1]['value'] or 0),
            'users':    int(row['metricValues'][2]['value'] or 0),
            'copies':   copy_map.get(path, 0),
            'cta':      cta_map.get(path, 0),
            'copies_jp': copy_map_jp.get(path, 0),
            'cta_jp':    cta_map_jp.get(path, 0),
        }
    return result

# ── AI経由流入（sessionSource から抽出） ──────────────────────────────────────
def fetch_ai_referrals(token, start_date, end_date):
    url = f'https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY}:runReport'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    body = {
        'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
        'dimensions': [{'name': 'sessionSource'}, {'name': 'sessionMedium'}],
        'metrics': [{'name': 'sessions'}],
        'limit': 200,
    }
    r = requests.post(url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    total = 0
    for row in data.get('rows', []):
        src = row['dimensionValues'][0]['value'].lower()
        med = row['dimensionValues'][1]['value'].lower()
        cnt = int(row['metricValues'][0]['value'] or 0)
        # GA4自身が ai-assistant と分類したものも拾う（新しいAI検索が増えても取りこぼさない）
        if med == 'ai-assistant' or any(k in src for k in AI_SOURCE_KEYWORDS):
            total += cnt
    return total

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

    print('[INFO] AI経由流入 集計中...')
    try:
        ai_sessions = fetch_ai_referrals(token, start_date, end_date)
        print(f'[INFO] AI経由セッション: {ai_sessions}')
    except Exception as e:
        print(f'[WARN] AI経由流入 取得失敗: {e}', file=sys.stderr)
        ai_sessions = 0

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
        g = ga4.get(path, {'pv': 0, 'sessions': 0, 'users': 0, 'copies': 0, 'cta': 0,
                           'copies_jp': 0, 'cta_jp': 0})
        s = gsc.get(path, {'impressions': 0, 'clicks': 0, 'ctr': 0, 'position': 0})
        cvr = g['copies'] / g['sessions'] if g['sessions'] > 0 else 0
        rows.append({
            'path':        path,
            'pv':          g['pv'],
            'sessions':    g['sessions'],
            'users':       g['users'],
            'copies':      g['copies'],
            'cta':         g.get('cta', 0),
            'copies_jp':   g.get('copies_jp', 0),
            'cta_jp':      g.get('cta_jp', 0),
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
    total_cta = sum(r['cta']     for r in rows)
    total_cop_jp = sum(r['copies_jp'] for r in rows)
    total_cta_jp = sum(r['cta_jp']    for r in rows)
    total_ai  = ai_sessions
    # 稼ぎ頭ページ数（月3クリック以上）＝主KPIを支える行動KPI（append_kpi_historyと同じ定義）
    total_earner_pages = sum(1 for r in rows if r['clicks'] >= 3)

    output = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'gsc_status':   gsc_status,
        'date_range':   {'start': start_date, 'end': end_date},
        'summary': {
            'total_pv':          total_pv,
            'total_impressions': total_imp,
            'total_clicks':      total_clk,
            'total_copies':      total_cop,
            'total_cta':         total_cta,
            'total_copies_jp':   total_cop_jp,
            'total_cta_jp':      total_cta_jp,
            'total_ai_sessions': total_ai,
            'total_earner_pages': total_earner_pages,
            'avg_ctr':           round(total_clk / total_imp, 6) if total_imp else 0,
            'avg_cvr':           round(total_cop / total_ses, 6) if total_ses else 0,
        },
        'rows': rows,
    }

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f'[INFO] 書き出し完了: {OUTPUT_FILE} ({len(rows)} ページ)')

    append_kpi_history(rows, output['summary'], gsc_status, total_ai)

# ── KPI日次履歴（CSV追記・1日1行） ─────────────────────────────────────────────
HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'kpi_history.csv')
KEY8 = ['/coupon-cj9852.html', '/waribiki.html', '/ryoukin.html', '/tsukaikata.html',
        '/touroku.html', '/amazon-sourcing.html', '/amazon-review-management.html',
        '/amazon-competitor-analysis.html']

def append_kpi_history(rows, summary, gsc_status, ai_sessions=0):
    import csv
    imp = summary['total_impressions']; clk = summary['total_clicks']
    cta = summary['total_cta']; cop = summary['total_copies']
    cta_jp = summary.get('total_cta_jp', 0); cop_jp = summary.get('total_copies_jp', 0)
    # 加重平均掲載順位
    wpos = sum(r['position'] * r['impressions'] for r in rows if r['impressions'] > 0)
    timp = sum(r['impressions'] for r in rows if r['impressions'] > 0)
    avg_pos = round(wpos / timp, 2) if timp else 0
    # 主要8ページの1ページ目カウント
    top10 = sum(1 for k in KEY8
                for r in [next((x for x in rows if x['path'] == k), None)]
                if r and 0 < r['position'] <= 10)
    ctr = round(clk / imp * 100, 2) if imp else 0
    cvr = round((cta + cop) / clk * 100, 2) if clk else 0
    # 復元記事コホート（2026-08-07 旧WordPress記事11本を復元・獲得エンジン検証）
    restored_imp = sum(r['impressions'] for r in rows if r['path'].startswith(('/2023/', '/2024/')))
    restored_clk = sum(r['clicks'] for r in rows if r['path'].startswith(('/2023/', '/2024/')))
    today = datetime.date.today().isoformat()

    # KPI6・7の正は _jp 列（日本限定＝bot除去後の実数）。無印は過去との連続性のため残す
    cvr_jp = round((cta_jp + cop_jp) / clk * 100, 2) if clk else 0

    # 稼ぎ頭ページ数（月3クリック以上）＝主KPI「検索クリック」を支える行動KPI（2026-08-11 戦略v7で新設）
    # 「1ページ目のページ数」は検索されないクエリで1位を取っても増える無意味な指標だったため降格。
    # 代わりに「実際にクリックを生んでいるページが何枚あるか」を追う（記事執筆・リライトと1対1で対応する）
    earner_pages = sum(1 for r in rows if r['clicks'] >= 3)

    header = ['date', 'gsc_status', 'impressions', 'avg_position', 'top10_pages',
              'ctr', 'clicks', 'cta_plus_copy', 'cvr', 'ai_sessions',
              'restored_imp', 'restored_clicks', 'cta_plus_copy_jp', 'cvr_jp', 'earner_pages']
    newrow = [today, gsc_status, imp, avg_pos, top10, ctr, clk, cta + cop, cvr, ai_sessions,
              restored_imp, restored_clk, cta_jp + cop_jp, cvr_jp, earner_pages]

    existing = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, newline='', encoding='utf-8') as f:
            existing = list(csv.reader(f))
    # 同日は上書き（1日1行を保証）、それ以外は追記
    body = [r + [''] * (len(header) - len(r)) for r in existing[1:] if r and r[0] != today]
    body.append([str(x) for x in newrow])
    with open(HISTORY_FILE, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(body)
    print(f'[INFO] KPI履歴 追記: {HISTORY_FILE} ({len(body)}行)')

if __name__ == '__main__':
    main()
