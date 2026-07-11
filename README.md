# RE:TANAKA価格 LINE WORKS配信

田中貴金属のRE:TANAKA価格を取得し、LINE WORKS Incoming Webhookへ通知します。

- K24特定品 リサイクル価格 (円/g)
- Pt特定品 リサイクル価格 (円/g)
- 前回営業日比
- 発表日時
- Xserver版では価格表スクリーンショットへのリンクボタン

## ローカル設定

~~~bash
cp .retanaka.env.example .retanaka.env
~~~

.retanaka.env にLINE WORKS Incoming Webhook URLを設定します。

~~~dotenv
LINEWORKS_WEBHOOK_URL=https://webhook.worksmobile.com/message/YOUR_WEBHOOK_ID
~~~

Webhook URLは認証情報です。リポジトリへコミットしないでください。

## 実行

送信せず確認:

~~~bash
python3 retanaka_line_bot.py --dry-run
~~~

LINE WORKSへ送信:

~~~bash
python3 retanaka_line_bot.py
~~~

テスト:

~~~bash
python3 -m unittest -q
~~~

## Xserver運用

Xserver共用サーバーでのCron運用手順は xserver/README_XSERVER.md を参照してください。

## セキュリティ

- 設定・状態ファイルは public_html の外へ置く
- config.json と状態ファイルはパーミッション 600
- public_html には公開前提の価格表画像だけを置く
- Incoming Webhook URL、ScreenshotOneキー、メールアドレス、サーバー固有値を公開しない
