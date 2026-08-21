"""定期削除処理（仕様 9.2）。cron から毎日 0:00 と 12:00（Asia/Tokyo）に実行する。

期限切れデータと削除待ちデータを物理削除する。
本文・共有キー・元ファイル名はログに残さない（仕様 15章）。
"""
import logging
import sys

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

    with db.session() as conn:
        purged = db.purge_old_rate_events(conn, now)
    db.vacuum()

    logger.info(
        "定期削除を完了しました 対象=%d 成功=%d 失敗=%d レート記録削除=%d",
        total,
        succeeded,
        failed,
        purged,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
