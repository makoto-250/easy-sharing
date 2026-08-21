"""画像サムネイルの生成（Pillow）。

- 対象: JPEG / PNG / WebP / GIF。PDF は対象外。
- 最大 600x600px、アスペクト比は保持。
- GIF・アニメーションは先頭フレームのみ。
- EXIF などのメタデータは引き継がない（回転だけ反映してから破棄）。
- Decompression Bomb・過大メモリ対策として画素数の上限を設ける。

生成に失敗しても呼び出し側は共有処理を止めず、プレビューなしで続行する。
"""
import io
import logging

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# サムネイルの最大寸法。
THUMB_MAX = (600, 600)

# サムネイルを作る MIME タイプ。PDF は含めない。
THUMBNAILABLE = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# Decompression Bomb 対策。ヘッダ上の画素数がこれを超えたら復号せず失敗させる。
# 例: 6300x6300 程度まで。ファイルサイズ上限(20MiB)を通っても展開後は巨大になり得る。
MAX_PIXELS = 40_000_000

# Pillow 自身のガードも合わせて厳しくする。
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
# 切り詰められた・壊れた画像を黙って受け入れない。
ImageFile.LOAD_TRUNCATED_IMAGES = False

# 出力フォーマット（PNG 固定。EXIF を持たず、透過も保持できる）。
OUTPUT_FORMAT = "PNG"
OUTPUT_MIME = "image/png"


def can_thumbnail(mime_type: str) -> bool:
    return mime_type in THUMBNAILABLE


def generate(source_path, mime_type: str) -> bytes | None:
    """保存済みの原本からサムネイル PNG のバイト列を返す。作れなければ None。"""
    if not can_thumbnail(mime_type):
        return None
    try:
        with Image.open(source_path) as img:
            # 寸法は基本ヘッダから得られる。復号前に画素数で門前払いする。
            width, height = img.size
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                logger.warning(
                    "サムネイル生成を中止しました（画素数超過） %dx%d", width, height
                )
                return None

            # アニメGIF等は先頭フレームのみ。
            try:
                img.seek(0)
            except EOFError:
                pass

            # EXIF の向きだけ反映してから、メタデータは引き継がない。
            oriented = ImageOps.exif_transpose(img)

            # 透過を保つ形式へ正規化する（P/LA/RGBA は RGBA、それ以外は RGB）。
            target_mode = "RGBA" if oriented.mode in ("RGBA", "LA", "P") else "RGB"
            oriented = oriented.convert(target_mode)

            oriented.thumbnail(THUMB_MAX)

            buffer = io.BytesIO()
            # exif を渡さないので EXIF は付かない。
            oriented.save(buffer, format=OUTPUT_FORMAT, optimize=True)
            return buffer.getvalue()
    except Image.DecompressionBombError:
        logger.warning("サムネイル生成を中止しました（Decompression Bomb） mime=%s", mime_type)
        return None
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning(
            "サムネイル生成に失敗しました mime=%s err=%s", mime_type, exc.__class__.__name__
        )
        return None
