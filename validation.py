"""共有キーとファイルの検証（仕様 4.3 / 4.4 / 13.1）。"""
import hashlib
import hmac
import re
import secrets
import unicodedata

import config

KEY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{%d}$" % config.SHARE_KEY_LENGTH)

# 拡張子 -> (許可MIMEタイプ, シグネチャ判定関数)
ALLOWED_TYPES = {
    ".pdf": "application/pdf",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

FORMAT_LABELS = {
    "application/pdf": "PDF",
    "image/gif": "GIF",
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WebP",
}


def generate_share_key() -> str:
    """暗号学的乱数で16文字を生成する。Math.random 相当は使わない（仕様 4.3）。"""
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(config.SHARE_KEY_LENGTH))


def normalize_share_key(raw: str | None) -> str:
    """前後の空白を除去する。大文字小文字は区別するので変換しない（仕様 4.3）。"""
    return (raw or "").strip()


def is_valid_share_key(key: str) -> bool:
    return bool(KEY_PATTERN.match(key))


def key_digest(share_key: str) -> str:
    """HMAC-SHA-256(共有キー, サーバー秘密鍵)。平文は保存しない（仕様 10.2）。"""
    return hmac.new(
        config.KEY_HMAC_SECRET.encode("utf-8"),
        share_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# --- ファイルシグネチャ ---------------------------------------------------


def _sniff_mime(head: bytes) -> str | None:
    """先頭バイト列から実際の形式を判定する。Content-Type や拡張子は信用しない。"""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _extension_of(filename: str) -> str:
    _, dot, ext = filename.rpartition(".")
    return f".{ext.lower()}" if dot else ""


class FileRejected(Exception):
    """利用者向けメッセージだけを持つ。詳細な検出理由は返さない（仕様 12章）。"""

    def __init__(self, message: str, field: str = "file"):
        super().__init__(message)
        self.message = message
        self.field = field


def validate_upload(storage, declared_mime: str | None) -> tuple[str, int, bytes]:
    """アップロードを検証し、(確定MIMEタイプ, サイズ, 先頭バイト) を返す。

    拡張子・MIMEタイプ・シグネチャの3点がすべて一致した場合のみ受け付ける。
    """
    filename = storage.filename or ""
    if not filename:
        raise FileRejected("共有するファイルを選択してください。")

    extension = _extension_of(filename)
    allowed_mime = ALLOWED_TYPES.get(extension)
    if allowed_mime is None:
        raise FileRejected("このファイル形式には対応していません。")

    # 申告された Content-Type が「存在し」「許可タイプと一致する」こと。
    # 空でも通してしまうと 3 点一致（拡張子・MIME・シグネチャ）が崩れるため、
    # 未申告も不一致として扱う（ただしこれ単独では判定しない）。
    declared = (declared_mime or "").split(";")[0].strip().lower()
    if declared != allowed_mime:
        raise FileRejected("このファイル形式には対応していません。")

    stream = storage.stream
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)

    if size == 0:
        raise FileRejected("共有するファイルを選択してください。")
    if size > config.MAX_FILE_BYTES:
        raise FileRejected("ファイルサイズは 20 MiB 以下にしてください。")

    head = stream.read(16)
    stream.seek(0)
    if _sniff_mime(head) != allowed_mime:
        raise FileRejected("このファイル形式には対応していません。")

    return allowed_mime, size, head


# --- 元ファイル名の無害化 -------------------------------------------------

_CONTROL_CHARS = dict.fromkeys(range(0x00, 0x20))
_CONTROL_CHARS[0x7F] = None


def sanitize_original_name(filename: str) -> str:
    """パス区切り・CR/LF・NULL・制御文字を除去する（仕様 13.1）。表示用のみに使う。"""
    name = unicodedata.normalize("NFC", filename or "")
    name = name.translate(_CONTROL_CHARS)
    name = name.replace("\\", "/").rpartition("/")[2]
    name = name.strip().strip(".")
    if not name:
        name = "file"
    return name[:255]


def format_label(mime_type: str) -> str:
    return FORMAT_LABELS.get(mime_type, mime_type)


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
