# セラースプライトLP 自動改善エージェント

## 毎回実行する手順

### STEP1：現状確認
curl -sL https://smkrrry-blip.github.io/sellersprite-lp-/ でHTMLを取得して以下を確認：
- title・metaディスクリプション・OGP・canonical・JSON-LDの有無
- コピーボタンの数（2個が正解）
- 比較表ヘッダーが4列か
- CTAボタンがファーストビューにあるか
- 画像のloading=lazy・altテキストの状態
- 表記統一（「公式代理店」に統一されているか）

### STEP2：問題点・不足点を全てリストアップ
SEO・CVR・デザイン・表記の観点で評価する

### STEP3：修正実行
発見した全問題点を index.html で修正する

### STEP4：git push
git -C ~/sellersprite-lp- add . && git -C ~/sellersprite-lp- commit -m "fix: auto improvement" && git -C ~/sellersprite-lp- push

### STEP5：デスクトップ通知
以下のコマンドで完了をmacOSデスクトップ通知で報告する：

```bash
python3 ~/sellersprite-lp-/send_report.py "修正内容の箇条書きテキスト"
```

または直接osascriptで実行：
```bash
osascript -e 'display notification "修正内容テキスト" with title "【LP自動改善完了】" subtitle "sellersprite-lp" sound name "Glass"'
```

## 固定情報
- 割引コード：CJ9852（30%OFF）
- 公式サイト：https://www.sellersprite.com/jp/w/user/login
- 表記統一：「セラースプライト公式代理店」
- LP URL：https://smkrrry-blip.github.io/sellersprite-lp-/
- 壮一さん写真：https://amazing-japan.jp/wp-content/uploads/2022/09/IMG_4969.jpg
