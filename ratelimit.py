"""IP 単位の試行回数制限（仕様 13.3）。

IP アドレスそのものは保持せず、秘密鍵付きハッシュの短期識別値だけを記録する
（仕様 15章）。記録は定期削除処理で 2 日を過ぎたぶんを消す。
"""
import hashlib
import hmac
from datetime import timedelta

import config
import db

SHARE = "share"
RECEIVE_FAIL = "receive_fail"
DOWNLOAD = "download"


def ip_hash(remote_addr: str | None) -> str:
    raw = (remote_addr or "unknown").encode("utf-8")
    return hmac.new(
        config.KEY_HMAC_SECRET.encode("utf-8"), raw, hashlib.sha256
    ).hexdigest()


def _count(conn, bucket: str, who: str, minutes: float) -> int:
    since = db.utcnow() - timedelta(minutes=minutes)
    return db.count_rate_events(conn, bucket, who, since)


def check_share(conn, who: str) -> bool:
    """共有処理: 10回/10分、50回/日。"""
    if _count(conn, SHARE, who, 10) >= config.RATE_SHARE_PER_10MIN:
        return False
    if _count(conn, SHARE, who, 60 * 24) >= config.RATE_SHARE_PER_DAY:
        return False
    return True


def record_share(conn, who: str) -> None:
    db.record_rate_event(conn, SHARE, who)


def check_receive(conn, who: str) -> bool:
    """受け取り失敗の制限（仕様 13.3）。

    「5回/分を超過したら15分間受け付けない」を、15分窓で失敗5回に到達したら
    ブロックする、と表現している。1分窓での超過は必ずこの条件も満たすため
    同じ効果になり、緩やかな総当たりも同時に抑えられる（安全側に倒す）。
    成功しても記録は消さないので、成功のみでは即時解除されない。
    """
    if _count(conn, RECEIVE_FAIL, who, config.RATE_RECEIVE_BLOCK_MINUTES) >= (
        config.RATE_RECEIVE_FAIL_PER_MIN
    ):
        return False
    if _count(conn, RECEIVE_FAIL, who, 60) >= config.RATE_RECEIVE_FAIL_PER_HOUR:
        return False
    return True


def record_receive_failure(conn, who: str) -> None:
    db.record_rate_event(conn, RECEIVE_FAIL, who)


def check_download(conn, who: str) -> bool:
    """ダウンロード: 60回/時。"""
    return _count(conn, DOWNLOAD, who, 60) < config.RATE_DOWNLOAD_PER_HOUR


def record_download(conn, who: str) -> None:
    db.record_rate_event(conn, DOWNLOAD, who)
