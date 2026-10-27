"""The category tree snapshot (pure once loaded)."""

from carousearch.carousell.categories import Category, CategoryTree, load_category_tree


def test_the_snapshot_has_the_motorcycle_licence_classes_under_used_and_new() -> None:
    tree = load_category_tree()

    assert tree.captured_on == "2026-10-27"
    assert [category.name for category in tree.children("7100")] == ["Class 2", "Class 2A", "Class 2B"]
    assert [category.id for category in tree.children("7200")] == ["7101", "7102", "7103"]
    assert [category.name for category in tree.path("1595")] == ["Motorcycles", "Used Motorcycles", "Class 2B"]
    assert tree.get("1595") == Category(id="1595", cc_id="1997", name="Class 2B", parent_id="7100")


def test_the_top_level_has_the_34_categories_of_the_site() -> None:
    names = {category.name for category in load_category_tree().top_level()}

    assert len(names) == 34 and {"Motorcycles", "Mobile Phones & Gadgets", "Furniture & Home Living"} <= names


def test_an_unknown_id_has_no_path_and_a_loop_in_the_data_does_not_hang() -> None:
    looped = CategoryTree([Category("1", "1", "A", "2"), Category("2", "2", "B", "1")], captured_on="test")

    assert load_category_tree().path("999999") == []
    assert [category.id for category in looped.path("1")] == ["2", "1"]
