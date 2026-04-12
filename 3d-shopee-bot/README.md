# 3D Shopee Bot — Bambu → Shopee 自動出品システム

## セットアップ手順

### 1. パッケージインストール
```bash
cd 3d-shopee-bot
pip install -r requirements.txt
```

### 2. 認証情報の設定
`config.py` を開いて以下を入力:

```
SHOPEE_PARTNER_ID   = "取得したPartner ID"
SHOPEE_PARTNER_KEY  = "取得したPartner Key"
SHOPEE_SHOP_ID      = "自動取得（下記参照）"
SHOPEE_ACCESS_TOKEN = "自動取得（下記参照）"
```

### 3. Shopeeアクセストークン取得
```bash
python auth_shopee.py
```
ブラウザが開くので、Shopeeにログインして認証してください。
config.pyに表示されたSHOP_IDとACCESS_TOKENをコピーしてください。

### 4. 動作確認（ドライラン）
```bash
python pipeline.py --dry-run
```

### 5. 本番実行
```bash
python pipeline.py
```

---

## コマンド一覧

```bash
# 全ステップ実行
python pipeline.py

# ドライラン（出品なし）
python pipeline.py --dry-run

# 個別ステップ
python pipeline.py --step scrape      # スクレイプのみ
python pipeline.py --step translate   # 翻訳のみ
python pipeline.py --step upload      # 画像アップロードのみ
python pipeline.py --step list        # 出品のみ
python pipeline.py --step status      # DB状態確認

# ダッシュボード
python dashboard.py
python dashboard.py list  # 出品済み一覧
```

---

## Shopee Open Platform 登録方法

1. https://open.shopee.com/ にアクセス
2. 「Become a Partner」からアカウント作成
3. 「My Apps」→「Create App」
4. App登録後、Partner IDとPartner Keyを取得
5. `python auth_shopee.py` で認証実行

---

## ファイル構成

```
3d-shopee-bot/
├── config.py          ← 認証情報・設定（要編集）
├── makerworld.py      ← MakerWorldスクレイパー
├── shopee_api.py      ← Shopee API クライアント
├── translator.py      ← AI翻訳・価格計算
├── db.py              ← SQLiteデータベース
├── pipeline.py        ← メインオーケストレーション
├── auth_shopee.py     ← Shopee OAuth認証
├── dashboard.py       ← 進捗ダッシュボード
├── requirements.txt   ← 依存パッケージ
└── data/
    └── products.db    ← SQLiteDB（自動生成）
```
