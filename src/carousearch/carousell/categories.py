"""Carousell's category tree, from a snapshot kept in the repo (category_tree.json).

The tree is only in the server-rendered search pages, about 2 MB each (access notes 4.8), so it is captured once
and read from the file rather than fetched per question. Each category has its collection `id` (what
SearchRequest.category takes), its `cc_id` (what search cards and `sfContext` report) and its parent.

`CategoryTree` is pure. `load_category_tree` reads the snapshot file once per process.
"""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

CATEGORY_TREE_FILE = Path(__file__).with_name("category_tree.json")


@dataclass(frozen=True)
class Category:
    id: str  # the collection id, e.g. "1595"
    cc_id: str
    name: str  # e.g. "Class 2B"
    parent_id: str | None  # None for a top-level category


class CategoryTree:
    def __init__(self, categories: list[Category], captured_on: str) -> None:
        self.captured_on = captured_on  # the snapshot date, e.g. "2026-10-27"
        self._by_id = {category.id: category for category in categories}

    def get(self, category_id: str) -> Category | None:
        return self._by_id.get(category_id)

    def top_level(self) -> list[Category]:
        return [category for category in self._by_id.values() if category.parent_id is None]

    def children(self, category_id: str) -> list[Category]:
        return [category for category in self._by_id.values() if category.parent_id == category_id]

    def path(self, category_id: str) -> list[Category]:
        """The category and its ancestors, top level first; empty for an id not in the tree."""
        path: list[Category] = []
        category = self.get(category_id)
        while category is not None and category not in path:
            path.insert(0, category)
            category = self.get(category.parent_id) if category.parent_id else None
        return path


def parse_category_tree(snapshot: dict) -> CategoryTree:
    categories = [
        Category(id=item["id"], cc_id=item["cc_id"], name=item["name"], parent_id=item["parent_id"])
        for item in snapshot["categories"]
    ]
    return CategoryTree(categories, captured_on=snapshot["captured_on"])


@cache
def load_category_tree(path: Path = CATEGORY_TREE_FILE) -> CategoryTree:
    return parse_category_tree(json.loads(path.read_text(encoding="utf-8")))
