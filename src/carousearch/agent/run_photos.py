"""Listing photos for the photo tools: which photo is a listing's main one, and a per-run file cache.

main_photo_url is pure: the full-size first photo when the run holds the listing's details, else the search card's
thumbnail. RunPhotoStore (I/O) keeps downloaded bytes under <run directory>/photos/, so judge_photos and
photo_metrics never download the same photo twice in one run.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

PHOTOS_DIRECTORY_NAME = "photos"
PhotoSource = Literal["full", "thumbnail"]


def main_photo_url(row: Mapping[str, Any]) -> tuple[str, PhotoSource] | None:
    """(url, source) for a listings.db row (decoded), or None when the row has no photo URL at all."""
    image_urls = row.get("image_urls")
    if isinstance(image_urls, list) and image_urls:
        return str(image_urls[0]), "full"
    thumbnail_url = row.get("thumbnail_url")
    if thumbnail_url:
        return str(thumbnail_url), "thumbnail"
    return None


class RunPhotoStore:
    def __init__(self, run_directory: Path) -> None:
        self.directory = run_directory / PHOTOS_DIRECTORY_NAME
        self.directory.mkdir(parents=True, exist_ok=True)

    def load(self, listing_id: str, source: PhotoSource) -> bytes | None:
        path = self._path(listing_id, source)
        return path.read_bytes() if path.exists() else None

    def save(self, listing_id: str, source: PhotoSource, data: bytes) -> None:
        self._path(listing_id, source).write_bytes(data)

    def _path(self, listing_id: str, source: PhotoSource) -> Path:
        return self.directory / f"{listing_id}_{source}.img"
