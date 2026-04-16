"""
Shopee 新規注文監視
Playwright headless + cookies.json でネットワーク応答をインターセプト
To Ship（未発送）注文数が増えたら macOS 通知
"""
import json
import os
from datetime import datetime
from pathlib import Path

BOT_DIR   = Path(__file__).parent
COOKIES_FILE = BOT_DIR / "cookies.json"
STATE_FILE   = BOT_DIR / "data" / "order_state.json"


def _notify(message: str):
    safe = message.replace("'", "\\'")
    os.system(
        f"osascript -e 'display notification \"{safe}\" "
        f"with title \"🛒 Shopee 新規注文\" sound name \"Glass\"'"
    )


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"to_ship_count": 0, "last_order_ids": []}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    state["checked_at"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def check_orders() -> dict:
    """
    Playwright headless でShopee Seller Center の注文ページを確認。
    ネットワーク応答をインターセプトして注文リストAPIのレスポンスを取得する。
    """
    from playwright.sync_api import sync_playwright

    if not COOKIES_FILE.exists():
        return {"error": "cookies.json なし — pipeline.py でログインしてください"}

    captured = []

    def on_response(response):
        url = response.url
        # Seller Center の注文リストAPIを捕捉（複数エンドポイント対応）
        if ("order" in url or "sale" in url) and response.status == 200:
            ct = response.headers.get("content-type", "")
            if "json" in ct:
                try:
                    body = response.json()
                    if body:
                        captured.append({"url": url, "body": body})
                except Exception:
                    pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            storage_state=str(COOKIES_FILE),
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="th-TH",
            timezone_id="Asia/Bangkok",
        )
        page = ctx.new_page()
        page.on("response", on_response)

        try:
            page.goto(
                "https://seller.shopee.co.th/portal/sale/list/all",
                timeout=30000,
                wait_until="domcontentloaded",
            )
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            if "login" in page.url:
                browser.close()
                return {"error": "セッション切れ"}

            # DOM から To Ship カウントも取得（バックアップ）
            dom_result = page.evaluate("""
                () => {
                    // ステータスタブのバッジ数を全て取る
                    const tabs = [...document.querySelectorAll('nav a, [role="tab"], [class*="tab-item"]')];
                    const counts = {};
                    tabs.forEach(t => {
                        const label = t.textContent.trim();
                        const m = label.match(/^(.+?)\s*\((\d+)\)$/);
                        if (m) counts[m[1].trim()] = parseInt(m[2]);
                    });
                    // 注文IDのリスト（テーブル内）
                    const idEls = [...document.querySelectorAll('[class*="order-id"], [data-order-id]')];
                    const ids = idEls.map(e => (e.textContent || e.getAttribute('data-order-id') || '').trim())
                                    .filter(Boolean).slice(0, 5);
                    return { tabCounts: counts, orderIds: ids };
                }
            """)

            browser.close()
            return {
                "captured_responses": len(captured),
                "api_data": captured[:3],
                "dom": dom_result,
            }

        except Exception as e:
            browser.close()
            return {"error": str(e)}


def _extract_to_ship_count(result: dict) -> tuple[int, list]:
    """APIレスポンスまたはDOMから To Ship 件数と注文IDリストを抽出"""
    to_ship = 0
    order_ids = []

    # DOM のタブカウントから
    dom = result.get("dom", {})
    tab_counts = dom.get("tabCounts", {})
    for key, val in tab_counts.items():
        kl = key.lower()
        if "ship" in kl or "process" in kl or "to" in kl:
            to_ship = max(to_ship, val)
    order_ids = dom.get("orderIds", [])

    # APIレスポンスから（より信頼性高い）
    for item in result.get("api_data", []):
        body = item.get("body", {})
        # Shopee Open Platform 形式: {"response": {"order_list": [...], "total_count": N}}
        resp = body.get("response") or body.get("data") or body
        if isinstance(resp, dict):
            total = resp.get("total_count") or resp.get("total") or resp.get("totalCount")
            if total is not None:
                to_ship = max(to_ship, int(total))
            items = resp.get("order_list") or resp.get("orders") or resp.get("list") or []
            for o in items[:5]:
                oid = (o.get("order_sn") or o.get("orderId") or o.get("order_id") or "")
                if oid:
                    order_ids.append(str(oid))

    return to_ship, order_ids


def main():
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] Shopee注文チェック")

    result = check_orders()

    if result.get("error"):
        err = result["error"]
        print(f"  ⚠️ {err}")
        if "切れ" in err:
            _notify("セッション切れ — 手動ログインが必要です")
        return

    state = _load_state()
    to_ship, order_ids = _extract_to_ship_count(result)
    prev_count = state.get("to_ship_count", 0)
    prev_ids   = set(state.get("last_order_ids", []))

    print(f"  To Ship: {to_ship}件（前回: {prev_count}件）")
    print(f"  注文IDs: {order_ids}")

    # 新着判定
    new_ids = [i for i in order_ids if i and i not in prev_ids]
    count_increased = to_ship > prev_count

    if new_ids or (count_increased and prev_count > 0):
        if new_ids:
            msg = f"新規注文 {len(new_ids)}件！ ID: {new_ids[0]}"
        else:
            diff = to_ship - prev_count
            msg = f"新規注文 +{diff}件（合計{to_ship}件）"
        _notify(msg)
        print(f"  🔔 通知: {msg}")

    # 状態更新
    state["to_ship_count"] = max(to_ship, prev_count)  # 減少は無視（発送済み）
    if order_ids:
        state["last_order_ids"] = list(prev_ids | set(order_ids))[-50:]  # 最大50件保持
    _save_state(state)


if __name__ == "__main__":
    main()
