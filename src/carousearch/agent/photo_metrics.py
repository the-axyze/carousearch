"""Zero-token measurements of a listing photo: size, sharpness and brightness. Pure: image bytes in, numbers out.

The photo is first scaled so its longer side is ANALYSIS_SIDE pixels, so photos of different sizes give comparable
numbers. Sharpness is the variance of the Laplacian of the grey image (Pillow's 3x3 kernel, no numpy needed):
sharp edges give large values, a blurred or out-of-focus photo small ones. The 8-bit filter clips each value to
-128..127, which caps the score near 16,000 but keeps the order. Brightness is the mean grey level, 0 to 255.
"""

from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageStat

from carousearch.photos import open_image

ANALYSIS_SIDE = 512
LAPLACIAN_KERNEL = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)
# Rough reading guides for the model, from generated test images and a handful of listing photos (estimates).
BLURRY_BELOW = 100.0
DARK_BELOW = 60.0
WASHED_OUT_ABOVE = 210.0


@dataclass(frozen=True)
class PhotoMetrics:
    width: int  # of the photo as downloaded
    height: int
    sharpness: float  # Laplacian variance at ANALYSIS_SIDE; under about BLURRY_BELOW reads as blurry
    brightness: float  # mean grey level, 0 (black) to 255 (white)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "width": self.width,
            "height": self.height,
            "sharpness": round(self.sharpness, 1),
            "brightness": round(self.brightness, 1),
        }


def measure_photo(image_bytes: bytes) -> PhotoMetrics:
    """Raises ValueError when the bytes are not an image Pillow can read."""
    image = open_image(image_bytes)
    grey = scale_to_side(image.convert("L"), ANALYSIS_SIDE)
    return PhotoMetrics(
        width=image.width,
        height=image.height,
        sharpness=laplacian_variance(grey),
        brightness=ImageStat.Stat(grey).mean[0],
    )


def scale_to_side(image: Image.Image, side: int) -> Image.Image:
    """The image scaled (up or down) so its longer side is `side` pixels, aspect ratio kept."""
    longer = max(image.width, image.height)
    if longer == side:
        return image
    size = (max(1, round(image.width * side / longer)), max(1, round(image.height * side / longer)))
    return image.resize(size, Image.Resampling.LANCZOS)


def laplacian_variance(grey: Image.Image) -> float:
    """Pillow leaves the outermost pixels unfiltered, so they are cropped before the variance is taken."""
    filtered = grey.filter(LAPLACIAN_KERNEL)
    if filtered.width > 2 and filtered.height > 2:
        filtered = filtered.crop((1, 1, filtered.width - 1, filtered.height - 1))
    return ImageStat.Stat(filtered).var[0]
