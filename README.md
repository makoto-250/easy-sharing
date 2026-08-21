# 簡易データ共有システム（easy-sharing）

アカウント不要。**16文字の共有キー**だけで、テキストまたはファイルを **12時間** 一時共有する Web システム。

- 共有できるもの: テキスト1件（最大100,000文字）または ファイル1件（PDF / GIF / JPEG / PNG / WebP、20 MiB まで）
- 受け取り期限: 共有完了から12時間。期限後は受け取り不可、次回の定期削除（毎日0時・12時）で物理削除
- 共有キーを知っている人は、受け取りと削除の両方ができる

- 公開URL: https://easy.ai-web-support.com
- リポジトリ: https://github.com/makoto-250/easy-sharing

## セットアップ（ローカル）

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# .env.example をコピーして SECRET_KEY / KEY_HMAC_SECRET を生成して埋める
python -c "import secrets; print(secrets.token_urlsafe(48))"

.venv/Scripts/python.exe app.py     # http://127.0.0.1:5040
```

## テスト

仕様書16章の受け入れ条件をそのまま実装してある。

```bash
.venv/Scripts/python.exe -m pytest test_acceptance.py -q
```

## デプロイ

`deploy/` に systemd ユニット・nginx 設定・cron 定義がある。手順は [CLAUDE.md](CLAUDE.md) を参照。

```bash
ssh root@160.251.183.195 '/root/easy-sharing/deploy/deploy.sh'
```

## 注意

簡易的な共有サービスです。機密情報・個人情報・流出すると問題になる情報には使用しないでください。
