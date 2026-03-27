#!/usr/bin/env python3
"""
LP自動改善完了 通知スクリプト（macOSデスクトップ通知 + Gmail）

使い方:
    python3 send_report.py "修正内容テキスト"

環境変数:
    GMAIL_APP_PASSWORD : Gmailアプリパスワード（~/.zshrcに設定済み）
"""

import smtplib
import subprocess
import sys
import os
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SENDER    = "smkrrry@gmail.com"
RECIPIENT = "smkrrry@gmail.com"
LP_URL    = "https://smkrrry-blip.github.io/sellersprite-lp-/"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def send_email(changes: str) -> None:
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not app_password:
        raise EnvironmentError("環境変数 GMAIL_APP_PASSWORD が未設定です。")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"【LP自動改善完了】{now}"

    body = f"""\
LP自動改善エージェントによる修正が完了しました。

■ 修正内容
{changes}

■ LP URL
{LP_URL}

---
このメールは自動送信されました。（{now}）
"""

    msg = MIMEMultipart()
    msg["From"]    = SENDER
    msg["To"]      = RECIPIENT
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(SENDER, app_password)
        smtp.sendmail(SENDER, RECIPIENT, msg.as_string())

    print(f"[メール] 送信完了 → {RECIPIENT}（件名: {subject}）")


def send_desktop_notification(changes: str) -> None:
    first_line = changes.replace("\\n", "\n").splitlines()[0] if changes else "修正完了"
    body = f"{first_line}  |  {LP_URL}"

    script = (
        f'display notification "{body}" '
        f'with title "【LP自動改善完了】" '
        f'subtitle "sellersprite-lp" '
        f'sound name "Glass"'
    )
    subprocess.run(["osascript", "-e", script], check=True)
    print(f"[通知] デスクトップ通知送信完了")


def notify(changes: str) -> None:
    # メール送信（失敗してもデスクトップ通知は続行）
    try:
        send_email(changes)
    except Exception as e:
        print(f"[メール] 送信失敗: {e}")

    # デスクトップ通知
    try:
        send_desktop_notification(changes)
    except Exception as e:
        print(f"[通知] デスクトップ通知失敗: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python3 send_report.py '修正内容テキスト'")
        sys.exit(1)

    notify(sys.argv[1])
