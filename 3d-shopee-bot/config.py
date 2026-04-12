"""
設定ファイル — ここに認証情報を入力してください
"""

# ─────────────────────────────────────────────
# Shopee セラーセンター ブラウザログイン情報
# ─────────────────────────────────────────────
SHOPEE_EMAIL    = "smkrrry@gmail.com"   # セラーセンターのログインメール
SHOPEE_PASSWORD = "Oriental1109/"      # セラーセンターのパスワード
SHOPEE_SELLER_URL = "https://seller.shopee.co.th"

BROWSER_SETTINGS = {
    "headless": False,            # 通常のChrome表示（True にするとヘッドレス）
    "slow_mo": 500,               # 操作間の基本遅延（ms）
    "daily_limit": 20,            # 1日の最大出品数
    "min_wait": 1.0,              # クリック間の最小待機（秒）
    "max_wait": 3.0,              # クリック間の最大待機（秒）
}

# ─────────────────────────────────────────────
# Shopee Open Platform 認証情報（API使用時のみ — 通常は不要）
# https://open.shopee.com でアプリ登録後に取得
# ─────────────────────────────────────────────
SHOPEE_PARTNER_ID   = ""          # 例: 1234567
SHOPEE_PARTNER_KEY  = ""          # 例: "abcdef1234567890abcdef..."
SHOPEE_SHOP_ID      = ""          # 例: 9876543
SHOPEE_ACCESS_TOKEN = ""          # OAuth後に取得

# 本番 or テスト
SHOPEE_ENV = "production"         # "production" or "sandbox"
SHOPEE_BASE_URL = {
    "production": "https://partner.shopeemobile.com",
    "sandbox":    "https://partner.test-stable.shopeemobile.com",
}

# ─────────────────────────────────────────────
# MakerWorld 設定
# ─────────────────────────────────────────────
MAKERWORLD_EMAIL    = ""          # MakerWorldアカウントのメール
MAKERWORLD_PASSWORD = ""          # パスワード（スクレイピング用）

# ─────────────────────────────────────────────
# AI翻訳 設定（どちらか1つを選ぶ）
# ─────────────────────────────────────────────
# オプション1: Claude API（高品質・有料）
ANTHROPIC_API_KEY   = ""          # sk-ant-...

# オプション2: Google Translate（無料枠あり）
GOOGLE_TRANSLATE_API_KEY = ""     # 空白でDeepL無料版を使用

# ─────────────────────────────────────────────
# 出品設定
# ─────────────────────────────────────────────
LISTING_SETTINGS = {
    "currency": "THB",
    "default_stock": 999,           # 無在庫なので大きめに設定
    "default_weight_kg": 0.1,       # デフォルト重量
    "default_days_to_ship": 7,      # 発送までの日数（印刷時間含む）
    "min_price_thb": 150,           # 最低価格 THB
    "markup_rate": 3.5,             # 原価の3.5倍（Shopee手数料・送料・利益含む）
    "condition": "NEW",
    "language": "th",               # 出品言語（タイ語）
    "include_english": True,        # 英語説明も追加するか
}

# フィラメント原価（1グラムあたりのコスト、THB）
FILAMENT_COST_PER_GRAM_THB = 1.5  # PLAフィラメント相場

# 印刷時間あたりコスト（電気代込み、THB/時間）
PRINT_COST_PER_HOUR_THB = 5.0

# ─────────────────────────────────────────────
# スクレイピング設定
# ─────────────────────────────────────────────
SCRAPING_SETTINGS = {
    "delay_min_sec": 2.0,           # リクエスト間の最小待機
    "delay_max_sec": 5.0,           # リクエスト間の最大待機
    "max_retries": 3,               # 失敗時のリトライ回数
    "items_per_run": 50,            # 1回の実行で処理する商品数
    "min_likes": 10,                # 最低いいね数（品質フィルター）
    "min_makes": 5,                 # 最低印刷実績数
    "commercial_use_only": True,    # 商業利用可のみ（重要！）
}

# ─────────────────────────────────────────────
# Shopee Thailand カテゴリマッピング
# ※ 正確なIDはShopee APIの get_category で確認してください:
#   python3 -c "from shopee_api import ShopeeAPI; import json; print(json.dumps(ShopeeAPI().get_category(), ensure_ascii=False, indent=2))"
# ─────────────────────────────────────────────
CATEGORY_MAP = {
    # MakerWorldカテゴリ → Shopee Thailand カテゴリID
    # งานฝีมือ / DIY / 3Dプリント関連
    "Hobby & Crafts":           101754,   # งานอดิเรกและงานฝีมือ
    "Home & Living":            101749,   # บ้านและสวน
    "Toys":                     101757,   # ของเล่น
    "Fashion":                  101687,   # แฟชั่น
    "Electronics":              101706,   # อิเล็กทรอนิกส์
    "Office":                   101748,   # เครื่องใช้สำนักงาน
    "Tools":                    101758,   # เครื่องมือและอุปกรณ์ก่อสร้าง
    "Education":                101752,   # หนังสือ
    "DEFAULT":                  101754,   # デフォルト: งานอดิเรก
}

# ─────────────────────────────────────────────
# Shopee Thailand 配送会社ロジスティクスID
# ※ 実際のIDはShopee APIの get_logistics で確認してください:
#   python3 -c "from shopee_api import ShopeeAPI; import json; print(json.dumps(ShopeeAPI()._get('/api/v2/logistics/get_logistics_channel_list'), ensure_ascii=False, indent=2))"
#
# 以下は Shopee Thailand の一般的な logistic_id（要API確認）
# ─────────────────────────────────────────────
LOGISTICS_CHANNELS = [
    {
        "logistic_id": 40001,       # Thailand Post (EMS) — 标准郵便
        "name": "Thailand Post EMS",
        "enabled": True,
        "is_free": False,
        "shipping_fee": 60.0,       # THB
    },
    {
        "logistic_id": 40009,       # Kerry Express Thailand
        "name": "Kerry Express",
        "enabled": True,
        "is_free": False,
        "shipping_fee": 50.0,
    },
    {
        "logistic_id": 40010,       # Flash Express
        "name": "Flash Express",
        "enabled": True,
        "is_free": False,
        "shipping_fee": 45.0,
    },
    {
        "logistic_id": 40011,       # J&T Express Thailand
        "name": "J&T Express",
        "enabled": True,
        "is_free": False,
        "shipping_fee": 45.0,
    },
    {
        "logistic_id": 40012,       # Shopee Express Standard
        "name": "Shopee Express Standard",
        "enabled": True,
        "is_free": False,
        "shipping_fee": 40.0,
    },
]

# デフォルトで有効にする配送会社（上記リストから選択）
DEFAULT_LOGISTICS_IDS = [40009, 40010, 40012]  # Kerry / Flash / Shopee Express
