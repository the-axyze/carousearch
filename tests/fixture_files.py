"""Load the saved real Carousell responses in tests/fixtures/ (refresh with `carousearch capture-fixtures`)."""

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SEARCH_FIXTURE_NAMES = ["search_price_condition", "search_category_fields"]


def load_json_fixture(file_name: str) -> Any:
    return json.loads((FIXTURES_DIR / file_name).read_text(encoding="utf-8"))


def load_text_fixture(file_name: str) -> str:
    return (FIXTURES_DIR / file_name).read_text(encoding="utf-8")


def listing_cards(search_response: Any) -> list[dict]:
    return [item["listingCard"] for item in search_response["data"]["results"] if "listingCard" in item]
