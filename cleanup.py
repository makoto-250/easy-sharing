"""定期削除処理（仕様 9.2）。cron から毎日 0:00 と 12:00（Asia/Tokyo）に実行する。

期限切れデータと削除待ちデータを物理削除する。
本文・共有キー・元ファイル名はログに残さない（仕様 15章）。
"""
import logging
import sys
import time

import config
import db
import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s cleanup %(message)s",
)
logger = logging.getLogger(__name__)


def run() -> int:
    config.validate()
    db.init_db()

    now = db.utcnow()
    with db.session() as conn:
        targets = db.collect_deletable(conn, now)

    total = len(targets)
    succeeded = 0
    failed = 0

    for row in targets:
        if storage.remove(row["storage_name"]):
            with db.session() as conn:
                db.delete_share(conn, row["id"])
            succeeded += 1
        else:
            # 次回の定期削除で再試行する。
            with db.session() as conn:
                db.mark_delete_pending(conn, row["id"])
            failed += 1
            logger.error("物理削除に失敗しました id=%s", row["id"])

    orphans = _sweep_orphans()

    with db.session() as conn:
        purged = db.purge_old_rate_events(conn, now)
    db.vacuum()

    logger.info(
        "定期削除を完了しました 対象=%d 成功=%d 失敗=%d 孤立ファイル削除=%d レート記録削除=%d",
        total,
        succeeded,
        failed,
        orphans,
        purged,
    )
    return 1 if failed else 0


def _sweep_orphans() -> int:
    """どのレコードからも参照されない保存ファイルを削除する（指摘#6）。

    共有作成の途中でDB登録に失敗し、かつファイル回収にも失敗した場合に残る
    孤立ファイルを回収する。作成直後の一瞬をDB未登録の正常ファイルと誤認
    しないよう、更新時刻が十分に古いものだけを対象にする。
    """
    storage_dir = config.UPLOAD_STORAGE_PATH
    if not storage_dir.exists():
        return 0
    with db.session() as conn:
        known = {r["storage_name"] for r in conn.execute("SELECT storage_name FROM shares")}
    cutoff = time.time() - 3600  # 1時間より古いものだけ
    removed = 0
    for path in storage_dir.iterdir():
        if not path.is_file() or path.name in known:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if storage.remove(path.name):
            removed += 1
            logger.error("孤立ファイルを削除しました name=%s", path.name)
    return removed


if __name__ == "__main__":
    sys.exit(run())
