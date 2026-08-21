# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概要

簡易データ共有システム。アカウント不要で、**16文字の共有キー**だけでテキスト1件または
ファイル1件（PDF/GIF/JPEG/PNG/WebP、20 MiBまで）を **12時間** 一時共有する Flask アプリ。

仕様書: [docs/simple_data_share_system_spec_v1.0.docx](docs/simple_data_share_system_spec_v1.0.docx)
実装判断で迷ったら仕様書の章番号を確認すること。コード中のコメントにも対応章を書いてある。

## 環境

| 項目 | 値 |
|---|---|
| 本番 | ConoHa VPS `160.251.183.195` (Ubuntu 22.04)、`ssh root@160.251.183.195` |
| 公開URL | https://easy.ai-web-support.com |
| リポジトリ | https://github.com/makoto-250/easy-sharing （PC・VPS 両方に origin 設定済み） |
| VPS パス | `/root/easy-sharing` |
| ポート | **5040**（127.0.0.1 のみ。nginx が proxy する） |
| DB | SQLite `/var/lib/easy-sharing/easy_sharing.db` |
| 保存領域 | `/var/lib/easy-sharing/uploads`（Web 公開ディレクトリ外・700） |
| Python | ローカル 3.14 / VPS 3.10、いずれも `.venv` |

**同じサーバーで `ai-web-support.com`（ポート5010）他が稼働中。nginx の既存 conf と
既存 systemd ユニットには触れないこと。** easy-sharing 用は独立したファイルにしてある。

VPS の他ポート使用状況: 5000, 5001, 5003, 5006-5008, 5010-5012, 5015, 5038

## 起動・操作コマンド

```bash
# ローカル開発（http、Secureクッキーなし）
.venv/Scripts/python.exe app.py          # http://127.0.0.1:5040

# 受け入れテスト（仕様書16章をそのまま実装したもの。46件）
.venv/Scripts/python.exe -m pytest test_acceptance.py -q

# VPS デプロイ（PC で commit & push したあとに実行する）
ssh root@160.251.183.195 '/root/easy-sharing/deploy/deploy.sh'

# VPS 個別操作
systemctl restart easy-sharing
journalctl -u easy-sharing -n 50 --no-pager
.venv/bin/python cleanup.py              # 定期削除を手動実行
```

## 開発ルール

- 各フェーズの実装完了後、自分で `git add . && git commit && git push` を実行すること。
  人間に「コミットしてください」と言わない。コミットメッセージは日本語でOK。
- **仕様を変更するときは docs/ の仕様書との差分を CLAUDE.md に必ず記録すること。**
- 挙動を変えたら `test_acceptance.py` も更新する。テストは仕様書の受け入れ条件 ID
  （F-01, K-02, V-03, E-02, S-04 …）と対応させてある。

## アーキテクチャ

```
app.py            # Flask ルート（S-01〜S-05）。検証 → 保存 → セッション発行
config.py         # 運用設定（仕様17章）。すべて環境変数で上書き可
db.py             # SQLite アクセス層。db.session() で必ず commit + close する
validation.py     # 共有キー生成/検証、HMAC ダイジェスト、ファイル署名検証
storage.py        # ファイル本体の保存・削除。ランダム64桁hex名、実行権限なし
ratelimit.py      # IP ハッシュ単位の試行回数制限
cleanup.py        # cron から叩く定期削除（0時・12時）
templates/        # S-01 share / S-02 share_done / S-03 receive
                  # S-04 receive_result / S-05 deleted / error
static/           # style.css, app.js（CSP が 'self' のためインラインJS/CSS禁止）
deploy/           # systemd unit, nginx conf, deploy.sh, crontab
```

### 設計上の重要な決定

**共有キーは平文をどこにも残さない。** DB に入るのは `HMAC-SHA-256(共有キー, KEY_HMAC_SECRET)`
の64桁hex（`key_digest`、UNIQUE）だけ。URL・Referer・ログにも含めない。
そのため **`KEY_HMAC_SECRET` を変更すると既存の共有キーが全部無効になる。**

**受け取りは2段階。** `POST /receive` でキーを検証 → 内部IDだけを短時間有効な
セッション（HttpOnly/Secure/SameSite）に入れて `/receive/result` へリダイレクト。
以降のダウンロード・削除は URL にキーを載せずセッションだけで認可する。

**期限判定は必ずサーバー側で毎回やり直す。** `db.find_active_by_id()` が
`status='active'` と `expires_at > now` を毎回チェックする。結果画面表示後に
期限が切れたらダウンロードも削除も 404 になる（受け入れ条件 S-05）。

**時刻は DB が UTC、表示と cron が Asia/Tokyo。** `db.to_db()` / `db.from_db()` を通すこと。

**ファイル検証は3点一致。** 拡張子・Content-Type・先頭バイトのシグネチャがすべて
一致した場合のみ受け付ける（`validation.validate_upload`）。1つでも欠けると
PHP を .jpg に改名したファイルが通る。

**物理削除は最大24時間遅れる。** 受け取り可能なのは12時間だが、物理削除は期限後の
次回定期処理（0時 or 12時）。画面で「12時間後に完全削除」とは書かないこと（仕様 9.2）。

**削除失敗時は `status='delete_pending'`。** 受け取り不可にした上で cleanup.py が再試行する。

## 環境変数（`.env`、git 管理外）

`.env.example` をコピーして使う。VPS では `/root/easy-sharing/.env`。

| 変数名 | 用途 |
|---|---|
| `SECRET_KEY` | Flask セッション署名 |
| `KEY_HMAC_SECRET` | 共有キーのダイジェスト生成。**変更すると既存キーが全無効** |
| `SHARE_TTL_HOURS` | 受け取り可能時間（既定12） |
| `MAX_TEXT_LENGTH` | テキスト上限（既定100000） |
| `MAX_FILE_BYTES` | ファイル上限（既定20971520 = 20 MiB） |
| `TOTAL_STORAGE_LIMIT` | 総保存容量。90%到達で新規ファイル共有を停止 |
| `UPLOAD_STORAGE_PATH` | ファイル保存先。**Web 公開ディレクトリの外** |
| `DATABASE_PATH` | SQLite ファイルの場所 |
| `FLASK_ENV` | `development` のときだけ Secure クッキーを外す |

`MAX_FILE_BYTES` を変えたら **nginx の `client_max_body_size` も合わせること**
（`deploy/nginx-easy.ai-web-support.com.conf`）。片方だけ変えると 413 の出方がずれる。

## 運用上の注意

- **共有データはバックアップ対象外**（仕様 14章）。`/var/lib/easy-sharing/` をバックアップに含めない。
- ログに共有キー平文・本文・ファイル内容・元ファイル名を出さないこと（仕様 15章）。
  `logger` に渡してよいのは内部ID・種別・サイズ・期限・エラーコードまで。
- 匿名アップロードを許可しているので、公開後は容量・アップロード件数・エラー率を確認する。
  必要なら CAPTCHA／サイト共通の利用コード／ウイルススキャンを追加する（仕様 13.4）。

## 仕様書からの実装判断（仕様18章「実装前に環境で確定する項目」）

仕様書が「環境で確定する」としていた項目の現在値。変更するときはここも更新すること。

| 項目 | 現在の設定 | 備考 |
|---|---|---|
| サービス名 | Easy Sharing | `SERVICE_NAME` で変更可 |
| ドメイン | easy.ai-web-support.com | |
| 配色・ロゴ | 青系のミニマル、ロゴなし | `static/style.css` の `:root` 変数 |
| TOTAL_STORAGE_LIMIT | 5 GiB | ディスク残 45GB に対して余裕をみた値 |
| 管理者通知 | ログ出力のみ | 容量90%到達時に `logger.error`。メール通知は未実装 |
| ログ保存期間 | journald の既定 | 変更していない |
| 公開範囲 | **完全公開** | 利用コードによる制限は未実装 |
| ウイルススキャン | **初版では未導入** | 仕様書でも将来拡張候補 |

## 仕様書との差分

| 箇所 | 仕様書 | 実装 | 理由 |
|---|---|---|---|
| 共有画面の注意書き・ファイル欄の補足（4.5） | 「20 MiB」 | **「20 MB」** | MiB が一般利用者に伝わりにくいため利用者向け文言だけ MB 表記にした。**実際の上限は 20 MiB（20,971,520 bytes）のまま変更していない。** エラー文言（12章「ファイルサイズは 20 MiB 以下にしてください。」）は仕様どおり MiB のまま |
