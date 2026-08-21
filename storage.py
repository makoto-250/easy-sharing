"""ファイル保存領域の操作（仕様 13.1）。

保存先は Web 公開ディレクトリの外。内部ファイル名は共有キーや元ファイル名から
生成せず、十分に長いランダム値を使う。
"""
import logging
import os
import secrets

import config

logger = logging.getLogger(__name__)


def ensure_storage_dir() -> None:
    config.UPLOAD_STORAGE_PATH.mkdir(parents=True, exist_ok=True)
    # 保存ディレクトリのパーミッションを所有者のみに制限する。
    os.chmod(config.UPLOAD_STORAGE_PATH, 0o700)


def new_storage_name() -> str:
    """拡張子を持たない 64 文字のランダム名。URL を推測できないようにする。"""
    return secrets.token_hex(32)


def path_for(storage_name: str):
    # storage_name は自前生成の hex のみ。念のため経路を検証する。
    if not storage_name or not all(c in "0123456789abcdef" for c in storage_name):
        raise ValueError("不正な保存ファイル名です")
    return config.UPLOAD_STORAGE_PATH / storage_name


def save(storage, storage_name: str) -> None:
    """アップロードを保存する。実行権限は与えない。"""
    ensure_storage_dir()
    target = path_for(storage_name)
    storage.stream.seek(0)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            while chunk := storage.stream.read(1024 * 1024):
                fh.write(chunk)
    except BaseException:
        remove(storage_name)
        raise


def remove(storage_name: str | None) -> bool:
    """削除に成功、または元から存在しなければ True。失敗したら False。"""
    if not storage_name:
        return True
    try:
        path_for(storage_name).unlink(missing_ok=True)
        return True
    except OSError:
        logger.exception("保存ファイルの削除に失敗しました name=%s", storage_name)
        return False


def open_for_read(storage_name: str):
    return open(path_for(storage_name), "rb")
