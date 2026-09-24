import hashlib
import warnings
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from .config import Settings
from .models import ImageInput, ServiceError

MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def validate_image(content: bytes, filename: str, settings: Settings) -> ImageInput:
    if not content or len(content) > settings.max_image_bytes:
        raise ServiceError("image_size", "图片为空或超过单图大小上限", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                mime = MIME.get(image.format)
                if not mime or getattr(image, "n_frames", 1) != 1:
                    raise ServiceError("image_format", "只接受单帧 JPEG、PNG、WebP")
                if image.width * image.height > settings.max_pixels:
                    raise ServiceError("image_pixels", "图片像素数超过上限", 413)
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()  # Detect truncated data before accepting the task.
    except ServiceError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise ServiceError("invalid_image", "图片内容无效或解码资源超限") from exc
    return ImageInput(hashlib.sha256(content).hexdigest(), mime, content, filename[:200])
