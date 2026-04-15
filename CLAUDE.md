# セラースプライトLP 自動改善エージェント

## 毎回実行する手順

### STEP1：現状確認
curl -sL https://smkrrry-blip.github.io/sellersprite-lp-/ でHTMLを取得して以下を確認：
- title・metaディスクリプション・OGP・canonical・JSON-LDの有無
- コピーボタンの数（計4個が正解：主要2 = hero/cta、補助2 = exit-intent/sticky）
  - hero: FV内、初回CV最強導線
  - cta: 中段CTAブロック、スクロール途中CV補強
  - exit-intent: PC幅>768pxで上端離脱検知時のモーダル、離脱対策
  - sticky: 画面下部固定バー、全画面でCV機会維持
  - 全ボタンに `gtag('event','code_copy',{event_label:'CJ9852'})` 設定済（GA4で個別計測可能）
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
