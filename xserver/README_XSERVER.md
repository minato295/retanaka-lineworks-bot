# Xserver運用手順

Xserver共用サーバーのCronからRE:TANAKA価格を毎分確認し、当日の最初の更新をLINE WORKS Incoming Webhookへ1回だけ通知します。

通知内容:

- K24特定品 / Pt特定品と前回営業日比
- 発表日時
- 取得元URL
- 価格表スクリーンショットへのリンクボタン

Incoming Webhookは画像メッセージを直接送信しないため、スクリーンショットは公開URLへのボタンとして表示します。

## ディレクトリ

非公開領域:

~~~text
/home/SERVER_ID/ops/retanaka-bot/retanaka_xserver_bot.py
/home/SERVER_ID/ops/retanaka-bot/config.json
/home/SERVER_ID/ops/retanaka-bot/storage/
~~~

公開領域:

~~~text
/home/SERVER_ID/DOMAIN/public_html/retanaka-bot/retanaka-bot-images/
~~~

public_html 配下はインターネットから閲覧できます。Webhook URL、APIキー、トークン、メールアドレス、個人情報を保存しないでください。

## 設定

config.example.json を config.json として非公開領域へ配置し、実値を設定します。

主要項目:

- lineworks_webhook_url: LINE WORKS Incoming Webhook URL
- state_file: 価格履歴と送信状態
- screenshotone_access_key: ScreenshotOne APIキー
- screenshot_public_dir: 公開画像ディレクトリ
- screenshot_public_base_url: 公開画像URL
- screenshot_retention_hours: 次回画像取得時に削除する画像の保持期限
- screenshot_delete_after_seconds: 送信後に同一プロセスで削除するまでの秒数。0 は即時削除なし

推奨権限:

~~~text
/home/SERVER_ID/ops/retanaka-bot                 700
config.json                                      600
storage/                                         700
storage/retanaka_price_state.json                600
retanaka_xserver_bot.py                          700
公開画像ディレクトリ                             755
公開画像                                         644
~~~

## テスト

送信せず取得内容を確認:

~~~bash
/usr/bin/python3.6 /home/SERVER_ID/ops/retanaka-bot/retanaka_xserver_bot.py \
  --config /home/SERVER_ID/ops/retanaka-bot/config.json \
  --dry-run
~~~

LINE WORKSへ1回だけ強制送信:

~~~bash
/usr/bin/python3.6 /home/SERVER_ID/ops/retanaka-bot/retanaka_xserver_bot.py \
  --config /home/SERVER_ID/ops/retanaka-bot/config.json \
  --force-send
~~~

## Cron

毎分実行:

~~~cron
* * * * * /usr/bin/python3.6 /home/SERVER_ID/ops/retanaka-bot/retanaka_xserver_bot.py --config /home/SERVER_ID/ops/retanaka-bot/config.json >> /home/SERVER_ID/ops/retanaka-bot/cron.log 2>&1
~~~

送信済みの日は価格ページへアクセスする前に終了します。同じ発表時刻の二重送信も防止します。
