# RE:TANAKA価格 LINE / LINE WORKS配信

田中貴金属のRE:TANAKA価格を取得し、次のルールで通知します。

- LINE: 当日の最初の新しい発表を1回だけ送信
- LINE WORKS: 新しい発表ごとに送信
- K24特定品、Pt特定品、銀(999)のリサイクル価格 (円/g)
- 発表日時と前回営業日比

Xserver版では、価格表のスクリーンショットを生成し、LINE WORKSには公開画像へのリンクボタン、LINEには画像メッセージとして添付します。

## ローカル設定

~~~bash
cp .retanaka.env.example .retanaka.env
~~~

`.retanaka.env` の設定名は次のとおりです。値は実環境で設定し、ファイルをコミットしないでください。

~~~dotenv
LINE_CHANNEL_ACCESS_TOKEN=YOUR_LINE_CHANNEL_ACCESS_TOKEN
LINE_GROUP_ID=YOUR_LINE_GROUP_ID
LINEWORKS_WEBHOOK_URL=YOUR_LINEWORKS_WEBHOOK_URL
~~~

## 実行

送信せず確認:

~~~bash
python3 retanaka_line_bot.py --dry-run
~~~

LINE WORKSだけを確認送信し、通常の状態を変更しない:

~~~bash
python3 retanaka_line_bot.py --test-lineworks-only
~~~

通常送信:

~~~bash
python3 retanaka_line_bot.py
~~~

テスト:

~~~bash
python3 -m unittest -q
~~~

## Xserver運用

Xserver共用サーバーで毎分Cronを実行する手順は [xserver/README_XSERVER.md](xserver/README_XSERVER.md) を参照してください。

## セキュリティ

- 設定、状態、ログ、ロック、実行コードは `public_html` の外へ置く
- 非公開ディレクトリは `700`、コードは `700`
- 設定・状態・ログ・ロックは `600`
- `public_html` には生成したランダム名の価格表画像だけを置く
- 公開ディレクトリは `755`、公開画像は `644`
- Webhook、LINEトークン、LINEグループID、ScreenshotOneキー、メールアドレス、ホスト固有値は公開しない
