"""Photo metrics on generated images (pure)."""

import io

import pytest
from PIL import Image, ImageDraw, ImageFilter

from carousearch.agent.photo_metrics import BLURRY_BELOW, DARK_BELOW, WASHED_OUT_ABOVE, measure_photo
from carousearch.photos import downsize_to_longest_side


def striped(width: int = 800, height: int = 600) -> Image.Image:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 20):
        draw.line([(x, 0), (x, height)], fill="black", width=3)
    return image


def jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def test_a_sharp_photo_scores_far_above_the_same_photo_blurred() -> None:
    sharp = measure_photo(jpeg(striped()))
    blurred = measure_photo(jpeg(striped().filter(ImageFilter.GaussianBlur(6))))

    assert sharp.sharpness > 1000 > BLURRY_BELOW > blurred.sharpness
    assert (sharp.width, sharp.height) == (800, 600)


def test_a_flat_photo_has_no_sharpness_at_all() -> None:
    assert measure_photo(jpeg(Image.new("RGB", (400, 300), (128, 128, 128)))).sharpness == pytest.approx(0, abs=1)


def test_brightness_tells_dark_from_washed_out() -> None:
    dark = measure_photo(jpeg(Image.new("RGB", (400, 300), (20, 20, 20))))
    bright = measure_photo(jpeg(Image.new("RGB", (400, 300), (240, 240, 240))))

    assert dark.brightness < DARK_BELOW
    assert bright.brightness > WASHED_OUT_ABOVE


def test_photos_are_compared_at_the_same_size() -> None:
    large = measure_photo(jpeg(striped(1600, 1200)))
    small = measure_photo(jpeg(striped(800, 600)))

    assert (large.width, large.height) == (1600, 1200)
    assert large.sharpness > BLURRY_BELOW and small.sharpness > BLURRY_BELOW


def test_metrics_round_for_the_model() -> None:
    assert measure_photo(jpeg(striped())).as_dict().keys() == {"width", "height", "sharpness", "brightness"}


def test_bytes_that_are_not_an_image_are_refused() -> None:
    with pytest.raises(ValueError, match="not a readable image"):
        measure_photo(b"<html>")


def test_a_photo_for_the_worker_fits_within_its_longest_side() -> None:
    portrait = downsize_to_longest_side(jpeg(striped(810, 1080)), 512)
    small = downsize_to_longest_side(jpeg(striped(240, 320)), 512)

    assert Image.open(io.BytesIO(portrait.data)).size == (384, 512)
    assert Image.open(io.BytesIO(small.data)).size == (240, 320)
