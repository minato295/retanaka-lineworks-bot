# Xserver運用手順

Xserver共用サーバーのCronからRE:TANAKA価格を毎分確認します。

- LINE: 当日の最初の新しい発表を1回だけ通知
- K24特定品、Pt特定品、銀(999)、前回営業日比、発表日時を通知
- 価格表画像を生成した場合、LINEには画像として添付

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

`public_html` 配下はインターネットから閲覧できます。公開領域には生成したランダム名の価格表画像だけを置き、APIキー、トークン、グループID、メールアドレス、個人情報、コード、設定、状態、ログは保存しません。

## 設定

`config.example.json` を `config.json` として非公開領域へ配置し、プレースホルダーを実値に置き換えます。

設定名は実装と一致させてください。

- 配信: `line_channel_access_token`, `line_group_id`
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

同じ発表時刻でも送信を強制する場合:

~~~bash
/usr/bin/python3.6 /home/YOUR_ACCOUNT/ops/retanaka-bot/retanaka_xserver_bot.py \
  --config /home/YOUR_ACCOUNT/ops/retanaka-bot/config.json \
  --force-send
~~~

## Cron

### シフト提出リマインド

`shift_reminder.enabled`を`true`にし、`users.user1`と`users.user2`へ対象者のLINEユーザーIDを設定すると、日本時間の毎月25日9時台に1回送信します。毎分Cronでは通常9:00に実行され、失敗時は9時台に同じリトライキーで再試行します。9時台を過ぎた遅延送信は行いません。

メッセージは`textV2`と`substitution`を使い、2人をメンションして「本日はシフト提出期限です。シフトの提出をお願いします。」と通知します。ユーザーIDは非公開設定だけに保存してください。価格配信とは別の月次送信状態を保持します。`--dry-run`ではリマインドを送信しません。

未認証アカウントではグループのユーザーID一覧APIを利用できません。必要な場合は`shift_mentions.php`を公開BOTディレクトリ、`shift_mentions_handler.php`を非公開のコード・設定ディレクトリへ配置します。公開側の入口に秘密値は含めません。

非公開設定の`line_channel_secret`でWebhook署名を検証し、`shift_reminder_capture_enabled: true`と`shift_reminder_capture_names: {"user1": "FIRST_DISPLAY_NAME", "user2": "SECOND_DISPLAY_NAME"}`で通常投稿者のIDを登録できます。対象グループだけを処理し、本文は保存しません。各投稿者のLINEプロフィール名と照合し、途中のIDは`shift_reminder_pending_users`へ保存します。2人が揃うと月次配信を有効にし、登録処理は終了します。同じ表示名で別IDを観測した場合は登録を拒否します。表示名だけでは本人性を完全には検証できないため、信頼するメンバーだけのグループで使用してください。

登録を開始しても、2人が通常投稿するまではメンション配信を開始できません。期限までにIDが揃わない場合、名前の文字列だけの通知へ自動変更したり、全員メンションで代替したりはしません。

毎分実行します。Cronのログも非公開領域に置きます。

~~~cron
* * * * * /usr/bin/python3.6 /home/YOUR_ACCOUNT/ops/retanaka-bot/retanaka_xserver_bot.py --config /home/YOUR_ACCOUNT/ops/retanaka-bot/config.json >> /home/YOUR_ACCOUNT/ops/retanaka-bot/cron.log 2>&1
~~~

発表時刻は状態ファイルで管理します。LINEは当日最初の発表だけ送信し、プロセスロックで重複実行を防ぎます。

通常の運用エラーと復旧はメールで通知します。本文には原因、BOTの動作、必要な対応、伏字済みの技術情報を含めます。復旧通知は元のエラー通知を `>` で引用し、返信ヘッダを付けて1回送ります。復旧後に同じ障害が再発した場合は、新しい障害として再度通知します。
