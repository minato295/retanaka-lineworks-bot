# Xserver運用手順

Xserver共用サーバーのCronからRE:TANAKA価格を毎分確認します。

- LINE: 当日の最初の新しい発表を1回だけ通知
- LINE WORKS: 新しい発表ごとに通知
- K24特定品、Pt特定品、銀(999)、前回営業日比、発表日時を通知
- 価格表画像を生成した場合、LINE WORKSには公開URLのリンクボタンを付け、LINEには画像として添付

## ディレクトリ

次のパスはすべて例です。`YOUR_ACCOUNT` と `YOUR_DOMAIN` を実環境の値に置き換えてください。

非公開領域:

~~~text
/home/YOUR_ACCOUNT/ops/retanaka-bot/retanaka_xserver_bot.py
/home/YOUR_ACCOUNT/ops/retanaka-bot/config.json
/home/YOUR_ACCOUNT/ops/retanaka-bot/storage/
/home/YOUR_ACCOUNT/ops/retanaka-bot/cron.log
~~~

公開領域:

~~~text
/home/YOUR_ACCOUNT/YOUR_DOMAIN/public_html/retanaka-bot-images/
~~~

`public_html` 配下はインターネットから閲覧できます。公開領域には生成したランダム名の価格表画像だけを置き、Webhook URL、APIキー、トークン、グループID、メールアドレス、個人情報、コード、設定、状態、ログは保存しません。

## 設定

`config.example.json` を `config.json` として非公開領域へ配置し、プレースホルダーを実値に置き換えます。

設定名は実装と一致させてください。

- 配信: `line_channel_access_token`, `line_group_id`, `lineworks_webhook_url`
- 価格・状態: `price_url`, `timezone`, `state_file`, `lock_file`
- アラート: `alert_email`, `alert_email_from`, `sendmail_path`
- 画像: `enable_section_screenshot`, `require_screenshot`, `screenshot_api_url`, `screenshotone_access_key`, `screenshot_public_dir`, `screenshot_public_base_url`, `screenshot_selector`, `screenshot_wait_for_selector`, `screenshot_hide_selectors`, `screenshot_retention_hours`, `screenshot_delete_after_seconds`

`state_file` と `lock_file` は非公開領域に置きます。`screenshot_public_dir` だけは公開画像ディレクトリを指定します。

## 推奨パーミッション

~~~text
/home/YOUR_ACCOUNT/ops/retanaka-bot                 700
retanaka_xserver_bot.py                              700
config.json                                          600
storage/                                              700
storage/retanaka_price_state.json                     600
storage/retanaka_price.lock                          600
cron.log                                              600
公開画像ディレクトリ                                  755
公開画像                                              644
~~~

## 実行確認

送信せず取得内容を確認:

~~~bash
/usr/bin/python3.6 /home/YOUR_ACCOUNT/ops/retanaka-bot/retanaka_xserver_bot.py \
  --config /home/YOUR_ACCOUNT/ops/retanaka-bot/config.json \
  --dry-run
~~~

LINE WORKSだけを1回テスト送信し、通常の状態を変更しない:

~~~bash
/usr/bin/python3.6 /home/YOUR_ACCOUNT/ops/retanaka-bot/retanaka_xserver_bot.py \
  --config /home/YOUR_ACCOUNT/ops/retanaka-bot/config.json \
  --test-lineworks-only
~~~

同じ発表時刻でも送信を強制する場合:

~~~bash
/usr/bin/python3.6 /home/YOUR_ACCOUNT/ops/retanaka-bot/retanaka_xserver_bot.py \
  --config /home/YOUR_ACCOUNT/ops/retanaka-bot/config.json \
  --force-send
~~~

## Cron

毎分実行します。Cronのログも非公開領域に置きます。

~~~cron
* * * * * /usr/bin/python3.6 /home/YOUR_ACCOUNT/ops/retanaka-bot/retanaka_xserver_bot.py --config /home/YOUR_ACCOUNT/ops/retanaka-bot/config.json >> /home/YOUR_ACCOUNT/ops/retanaka-bot/cron.log 2>&1
~~~

発表時刻は状態ファイルで管理します。LINEは当日最初の発表だけ、LINE WORKSは新しい発表ごとに送信し、プロセスロックで重複実行を防ぎます。

価格取得、設定、画像取得、LINE、LINE WORKSの運用エラーは日本語メールで通知します。本文には原因、BOTの動作、必要な対応、伏字済みの技術情報を含めます。対象処理が正常に戻ると、元のエラーメールを `>` で引用し、返信ヘッダを付けた復旧メールを1回送ります。復旧後に同じ障害が再発した場合は、新しい障害として再度通知します。
