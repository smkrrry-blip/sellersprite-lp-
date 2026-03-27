#!/usr/bin/env python3
"""
LP自動改善完了 デスクトップ通知スクリプト（macOS）

使い方:
    python3 send_report.py "修正内容テキスト"
    python3 send_report.py "- CTAボタンを追加\n- altテキストを修正"
"""

import subprocess
import sys

TITLE = "【LP自動改善完了】"
SUBTITLE = "sellersprite-lp"
SOUND = "Glass"
LP_URL = "https://smkrrry-blip.github.io/sellersprite-lp-/"


def notify(changes: str) -> None:
    # 通知本文：修正内容の最初の1行 + URL（osascriptは長文を切り詰めるため簡潔に）
    first_line = changes.replace("\\n", "\n").splitlines()[0] if changes else "修正完了"
    body = f"{first_line}  |  {LP_URL}"

    script = (
        f'display notification "{body}" '
        f'with title "{TITLE}" '
        f'subtitle "{SUBTITLE}" '
        f'sound name "{SOUND}"'
    )
    subprocess.run(["osascript", "-e", script], check=True)
    print(f"通知送信完了: {TITLE}")
    print(f"内容: {body}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python3 send_report.py '修正内容テキスト'")
        sys.exit(1)

    notify(sys.argv[1])
