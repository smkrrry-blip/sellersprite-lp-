#!/usr/bin/env python3
"""
LP自動改善完了メール送信スクリプト

使い方:
    python send_report.py "修正内容テキスト"
    python send_report.py "- CTAボタンを追加\n- altテキストを修正"

環境変数:
    GMAIL_APP_PASSWORD : GmailアプリパスワードをセットしてからRUN
"""

import smtplib
import sys
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SENDER = "smkrrry@gmail.com"
RECIPIENT = "smkrrry@gmail.com"
SUBJECT = "【LP自動改善完了】修正内容レポート"
LP_URL = "https://smkrrry-blip.github.io/sellersprite-lp-/"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def send_report(changes: str) -> None:
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not app_password:
        raise EnvironmentError(
            "環境変数 GMAIL_APP_PASSWORD が設定されていません。\n"
            "export GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx' を実行してください。"
        )

    body = f"""\
LP自動改善エージェントによる修正が完了しました。

■ 修正内容
{changes}

■ LP URL
{LP_URL}

---
このメールは自動送信されました。
"""

    msg = MIMEMultipart()
    msg["From"] = SENDER
    msg["To"] = RECIPIENT
    msg["Subject"] = SUBJECT
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(SENDER, app_password)
        smtp.sendmail(SENDER, RECIPIENT, msg.as_string())

    print(f"送信完了: {RECIPIENT}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python send_report.py '修正内容テキスト'")
        sys.exit(1)

    changes_text = sys.argv[1].replace("\\n", "\n")
    send_report(changes_text)


# ============================================================
# GmailアプリパスワードはGoogleアカウントの設定から取得できます
#
# 手順：
#   1. Googleアカウント (myaccount.google.com) を開く
#   2. 「セキュリティ」→「2段階認証プロセス」を有効にする
#   3. 「2段階認証プロセス」ページ最下部の「アプリパスワード」を開く
#   4. アプリ名を任意で入力（例: "LP_agent"）→「作成」
#   5. 表示された16文字のパスワード（スペース含む）をコピー
#   6. 実行前に以下をターミナルで設定：
#      export GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx'
#
# 注意：
#   - アプリパスワードはGoogleアカウントのパスワードとは別物です
#   - 2段階認証が有効になっていないと「アプリパスワード」は表示されません
#   - パスワードをソースコードに直書きしないでください
# ============================================================
