import io

import pytest
from PIL import Image

from carousearch.photos import downsize_photo


def image_bytes(width: int, height: int, image_format: str, mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), color="white").save(buffer, format=image_format)
    return buffer.getvalue()


def size_and_format(data: bytes) -> tuple[tuple[int, int], str]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size, image.format


def test_a_wide_photo_is_scaled_to_max_width_keeping_its_aspect_ratio() -> None:
    photo = downsize_photo(image_bytes(1600, 1200, "JPEG"), max_width=800)

    assert photo.format == "jpeg"
    assert size_and_format(photo.data) == ((800, 600), "JPEG")


def test_a_photo_no_wider_than_max_width_is_returned_unchanged() -> None:
    original = image_bytes(800, 1000, "PNG")

    photo = downsize_photo(original, max_width=800)

    assert photo.data == original
    assert photo.format == "png"


def test_a_downsized_png_stays_png() -> None:
    photo = downsize_photo(image_bytes(1000, 500, "PNG", mode="RGBA"), max_width=400)

    assert photo.format == "png"
    assert size_and_format(photo.data) == ((400, 200), "PNG")


def test_formats_hosts_may_not_accept_are_reencoded_as_jpeg() -> None:
    photo = downsize_photo(image_bytes(300, 200, "BMP"), max_width=800)

    assert photo.format == "jpeg"
    assert size_and_format(photo.data) == ((300, 200), "JPEG")


def test_bytes_that_are_not_an_image_raise_value_error() -> None:
    with pytest.raises(ValueError, match="not a readable image"):
        downsize_photo(b"<html>not a photo</html>", max_width=800)


def test_max_width_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_width"):
        downsize_photo(image_bytes(10, 10, "PNG"), max_width=0)
