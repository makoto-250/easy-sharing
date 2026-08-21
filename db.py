"""SQLite アクセス層。SQL はすべてプレースホルダを使う（仕様 13.2）。"""
import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
    id            TEXT PRIMARY KEY,
    key_digest    TEXT NOT NULL UNIQUE,
    data_type     TEXT NOT NULL CHECK (data_type IN ('text', 'file')),
    text_body     TEXT,
    storage_name  TEXT,
    thumb_name    TEXT,
    original_name TEXT,
    mime_type     TEXT,
    file_size     INTEGER,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'delete_pending')),
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shares_expires ON shares (expires_at);
CREATE INDEX IF NOT EXISTS idx_shares_status  ON shares (status);

-- レート制限用。IP は短期識別値（ハッシュ）で保持する（仕様 15章）。
CREATE TABLE IF NOT EXISTS rate_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket     TEXT NOT NULL,
    ip_hash    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_lookup
    ON rate_events (bucket, ip_hash, created_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_db(dt: datetime) -> str:
    """UTC の ISO8601 文字列へ。DB には常に UTC で保存する（仕様 9.1）。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def from_db(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)


def connect() -> sqlite3.Connection:
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextlib.contextmanager
def session():
    """コミット/ロールバックし、必ず接続を閉じる。

    sqlite3.Connection をそのまま with に渡すとコミットはされるが閉じられないため、
    アプリ側では常にこちらを使う。
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextlib.contextmanager
def immediate():
    """BEGIN IMMEDIATE で書き込みロックを取ってから処理する。

    「件数を数えてから挿入する」ような確認＋書き込みを、他ワーカーに割り込まれず
    アトミックに行うために使う（容量制限・レート制限の並行すり抜け対策）。
    SQLite は書き込みを1つに直列化するため、ロック取得後の COUNT は確定値になる。
    """
    conn = connect()
    conn.isolation_level = None  # 自動 BEGIN を止めて手動制御する
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """既存DBに後から追加した列を補う（CREATE TABLE IF NOT EXISTS では追加されない）。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(shares)")}
    if "thumb_name" not in columns:
        conn.execute("ALTER TABLE shares ADD COLUMN thumb_name TEXT")


def vacuum() -> None:
    """VACUUM はトランザクション内で実行できないため専用の接続で行う。"""
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("VACUUM")
    finally:
        conn.close()


# --- 共有データ -----------------------------------------------------------


def insert_share(conn: sqlite3.Connection, record: dict) -> None:
    """key_digest が重複した場合は sqlite3.IntegrityError を送出する。"""
    conn.execute(
        """
        INSERT INTO shares (id, key_digest, data_type, text_body, storage_name,
                            thumb_name, original_name, mime_type, file_size, status,
                            created_at, expires_at)
        VALUES (:id, :key_digest, :data_type, :text_body, :storage_name,
                :thumb_name, :original_name, :mime_type, :file_size, :status,
                :created_at, :expires_at)
        """,
        record,
    )


def find_active_by_digest(conn: sqlite3.Connection, key_digest: str):
    """status=active かつ未期限のレコードだけを返す（仕様 11.2）。"""
    return conn.execute(
        """
        SELECT * FROM shares
         WHERE key_digest = ? AND status = 'active' AND expires_at > ?
        """,
        (key_digest, to_db(utcnow())),
    ).fetchone()


def find_active_by_id(conn: sqlite3.Connection, share_id: str):
    """各処理の直前に期限と状態を再確認するために使う（仕様 7.1 / 11.3）。"""
    return conn.execute(
        """
        SELECT * FROM shares
         WHERE id = ? AND status = 'active' AND expires_at > ?
        """,
        (share_id, to_db(utcnow())),
    ).fetchone()


def digest_exists(conn: sqlite3.Connection, key_digest: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM shares WHERE key_digest = ?", (key_digest,)
    ).fetchone()
    return row is not None


def delete_share(conn: sqlite3.Connection, share_id: str) -> None:
    conn.execute("DELETE FROM shares WHERE id = ?", (share_id,))


def mark_delete_pending(conn: sqlite3.Connection, share_id: str) -> None:
    """ファイル削除に失敗したときの退避状態（仕様 8.3）。受け取り不可になる。"""
    conn.execute(
        "UPDATE shares SET status = 'delete_pending' WHERE id = ?", (share_id,)
    )


def expires_at_for(created_at: datetime) -> datetime:
    return created_at + timedelta(hours=config.SHARE_TTL_HOURS)


def total_stored_bytes(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(file_size), 0) AS total FROM shares WHERE data_type = 'file'"
    ).fetchone()
    return int(row["total"])


def collect_deletable(conn: sqlite3.Connection, now: datetime) -> list:
    """定期削除の対象: 期限切れ、または削除待ち（仕様 9.2）。"""
    return conn.execute(
        """
        SELECT * FROM shares
         WHERE expires_at <= ? OR status = 'delete_pending'
        """,
        (to_db(now),),
    ).fetchall()


# --- レート制限 -----------------------------------------------------------


def record_rate_event(conn: sqlite3.Connection, bucket: str, ip_hash: str) -> None:
    conn.execute(
        "INSERT INTO rate_events (bucket, ip_hash, created_at) VALUES (?, ?, ?)",
        (bucket, ip_hash, to_db(utcnow())),
    )


def count_rate_events(
    conn: sqlite3.Connection, bucket: str, ip_hash: str, since: datetime
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM rate_events
         WHERE bucket = ? AND ip_hash = ? AND created_at >= ?
        """,
        (bucket, ip_hash, to_db(since)),
    ).fetchone()
    return int(row["c"])


def clear_rate_events(conn: sqlite3.Connection, bucket: str, ip_hash: str) -> None:
    conn.execute(
        "DELETE FROM rate_events WHERE bucket = ? AND ip_hash = ?", (bucket, ip_hash)
    )


def purge_old_rate_events(conn: sqlite3.Connection, now: datetime) -> int:
    """レート制限以外の目的で IP 情報を長期保持しない（仕様 15章）。"""
    cutoff = now - timedelta(days=2)
    cur = conn.execute("DELETE FROM rate_events WHERE created_at < ?", (to_db(cutoff),))
    return cur.rowcount
