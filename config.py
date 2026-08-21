"""運用設定（仕様書 17章）。すべて環境変数で上書きできる。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


# --- 秘密情報 -------------------------------------------------------------
# KEY_HMAC_SECRET は共有キーの検索用ダイジェスト生成に使う。ソース・DBへ保存しない。
KEY_HMAC_SECRET = os.environ.get("KEY_HMAC_SECRET", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")

# --- 共有仕様 -------------------------------------------------------------
SHARE_TTL_HOURS = _int("SHARE_TTL_HOURS", 12)
MAX_TEXT_LENGTH = _int("MAX_TEXT_LENGTH", 100_000)
MAX_FILE_BYTES = _int("MAX_FILE_BYTES", 20 * 1024 * 1024)  # 20 MiB
SHARE_KEY_LENGTH = 16

# リバースプロキシ/Flask 側のリクエスト上限は 20 MiB を「わずかに上回る」値にする（仕様 13.1）。
# 超過分はフォームのオーバーヘッド用。
MAX_CONTENT_LENGTH = MAX_FILE_BYTES + 256 * 1024

# --- 保存領域 -------------------------------------------------------------
# 本番では Web 公開ディレクトリの外（例: /var/lib/easy-sharing/uploads）を指定する。
UPLOAD_STORAGE_PATH = Path(
    os.environ.get("UPLOAD_STORAGE_PATH", BASE_DIR / "var" / "uploads")
).resolve()
DATABASE_PATH = Path(
    os.environ.get("DATABASE_PATH", BASE_DIR / "var" / "easy_sharing.db")
).resolve()

# 総容量上限。90% 到達で新規ファイル共有を停止する（仕様 13.3）。
TOTAL_STORAGE_LIMIT = _int("TOTAL_STORAGE_LIMIT", 5 * 1024 * 1024 * 1024)  # 5 GiB
STORAGE_WARN_RATIO = 0.9

# --- 受け取りセッション ---------------------------------------------------
# 受け取り結果画面を開いていられる時間（秒）。短時間有効（仕様 11.2）。
RECEIVE_SESSION_SECONDS = _int("RECEIVE_SESSION_SECONDS", 30 * 60)

# --- レート制限（仕様 13.3）----------------------------------------------
RATE_SHARE_PER_10MIN = _int("RATE_SHARE_PER_10MIN", 10)
RATE_SHARE_PER_DAY = _int("RATE_SHARE_PER_DAY", 50)
RATE_RECEIVE_FAIL_PER_MIN = _int("RATE_RECEIVE_FAIL_PER_MIN", 5)
RATE_RECEIVE_FAIL_PER_HOUR = _int("RATE_RECEIVE_FAIL_PER_HOUR", 30)
RATE_RECEIVE_BLOCK_MINUTES = _int("RATE_RECEIVE_BLOCK_MINUTES", 15)
RATE_DOWNLOAD_PER_HOUR = _int("RATE_DOWNLOAD_PER_HOUR", 60)

# --- 表示 -----------------------------------------------------------------
SERVICE_NAME = os.environ.get("SERVICE_NAME", "Easy Sharing")
DISPLAY_TIMEZONE = "Asia/Tokyo"

# --- 動作環境 -------------------------------------------------------------
# 本番（HTTPS）では Secure 属性付きクッキーを使う。
IS_PRODUCTION = os.environ.get("FLASK_ENV", "production").lower() != "development"


class ConfigError(RuntimeError):
    pass


def validate() -> None:
    """起動時に必須の秘密情報が設定されているか確認する。

    SECRET_KEY が弱いとセッション Cookie を偽造され、任意の共有 ID へ
    アクセスされうるため、KEY_HMAC_SECRET と同じく十分な長さを要求する。
    """
    missing = [n for n in ("KEY_HMAC_SECRET", "SECRET_KEY") if not globals()[n]]
    if missing:
        raise ConfigError(
            f"必須の環境変数が未設定です: {', '.join(missing)} / .env.example を参照してください"
        )
    for name in ("KEY_HMAC_SECRET", "SECRET_KEY"):
        if len(globals()[name]) < 32:
            raise ConfigError(
                f"{name} は32文字以上のランダム文字列にしてください "
                '（例: python -c "import secrets; print(secrets.token_urlsafe(48))"）'
            )
