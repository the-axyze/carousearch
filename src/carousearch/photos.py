"""Listing photos made small enough for a host model to look at. Pure: image bytes in, image bytes out."""

import io
from dataclasses import dataclass

from PIL import Image

# Formats model hosts accept as image input. A photo in any other format is re-encoded as JPEG.
PASS_THROUGH_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
JPEG_QUALITY = 85


@dataclass(frozen=True)
class Photo:
    data: bytes
    format: str  # lowercase format name, "jpeg", "png" or "webp"; image/<format> is its MIME type


def downsize_photo(image_bytes: bytes, max_width: int) -> Photo:
    """The photo scaled down to `max_width` pixels wide, aspect ratio kept, when it is wider; otherwise as it came.

    Raises ValueError when the bytes are not an image Pillow can read.
    """
    if max_width < 1:
        raise ValueError(f"max_width must be at least 1, got {max_width}")
    image = open_image(image_bytes)
    source_format = image.format or ""
    if image.width <= max_width and source_format in PASS_THROUGH_FORMATS:
        return Photo(data=image_bytes, format=source_format.lower())
    if image.width > max_width:
        new_size = (max_width, scaled_height(image.width, image.height, max_width))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    output_format = source_format if source_format in PASS_THROUGH_FORMATS else "JPEG"
    return Photo(data=encode_image(image, output_format), format=output_format.lower())


def open_image(image_bytes: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except (OSError, Image.DecompressionBombError) as error:  # UnidentifiedImageError is an OSError
        raise ValueError(f"not a readable image: {error}") from error
    return image


def scaled_height(width: int, height: int, new_width: int) -> int:
    return max(1, round(height * new_width / width))


def encode_image(image: Image.Image, image_format: str) -> bytes:
    if image_format == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")  # JPEG has no alpha channel or palette
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, quality=JPEG_QUALITY)
    return buffer.getvalue()
