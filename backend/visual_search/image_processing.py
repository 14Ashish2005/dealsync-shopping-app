from __future__ import annotations

import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 20_000_000
MAX_NORMALIZED_DIMENSION = 2048

_SUPPORTED_FORMATS = {
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".png": ("PNG", "image/png"),
    ".webp": ("WEBP", "image/webp"),
}
_JPEG_MEDIA_TYPES = {"image/jpeg", "image/jpg"}


class ImageValidationError(ValueError):
    pass


class ImageCleanupError(OSError):
    pass


@dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    media_type: str
    width: int
    height: int


def _validate_header(image: Image.Image, expected_format: str) -> None:
    if image.format != expected_format:
        raise ImageValidationError(
            "The file extension does not match the image content."
        )
    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
        raise ImageValidationError("Animated or multi-frame images are not supported.")

    width, height = image.size
    if (
        width <= 0
        or height <= 0
        or width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise ImageValidationError("Image dimensions exceed the supported limits.")


def normalize_image_bytes(
    image_data: bytes,
    *,
    filename: str,
    media_type: str | None = None,
) -> NormalizedImage:
    if len(image_data) > MAX_UPLOAD_SIZE_BYTES:
        raise ImageValidationError("Image must be no larger than 10 MB.")

    extension = Path(filename).suffix.lower()
    expected = _SUPPORTED_FORMATS.get(extension)
    if expected is None:
        raise ImageValidationError("Please upload a JPG, PNG, or WebP image.")

    expected_format, expected_media_type = expected
    if media_type:
        declared_type = media_type.split(";", 1)[0].strip().lower()
        allowed_types = (
            _JPEG_MEDIA_TYPES if expected_format == "JPEG" else {expected_media_type}
        )
        if (
            declared_type not in allowed_types
            and declared_type != "application/octet-stream"
        ):
            raise ImageValidationError(
                "The declared media type does not match the image extension."
            )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image_data)) as image:
                _validate_header(image, expected_format)
                image.verify()

            with Image.open(BytesIO(image_data)) as image:
                _validate_header(image, expected_format)
                image.load()
                oriented = ImageOps.exif_transpose(image)
                if "A" in oriented.getbands() or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    rgb = Image.new("RGB", rgba.size, (255, 255, 255))
                    rgb.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    rgb = oriented.convert("RGB")

                rgb.thumbnail(
                    (MAX_NORMALIZED_DIMENSION, MAX_NORMALIZED_DIMENSION),
                    Image.Resampling.LANCZOS,
                )
                output = BytesIO()
                rgb.save(output, format="JPEG", quality=85, optimize=True)
                width, height = rgb.size
                return NormalizedImage(
                    data=output.getvalue(),
                    media_type="image/jpeg",
                    width=width,
                    height=height,
                )
    except ImageValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
    ):
        raise ImageValidationError("The uploaded file is not a valid supported image.")


def cleanup_temporary_image(path: str | Path | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        raise ImageCleanupError("A temporary uploaded image could not be removed.") from None
