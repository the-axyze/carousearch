"""The owner's knowledge notes: parsing, selection by keyword, formatting and saving."""

from pathlib import Path

from carousearch.agent.knowledge import (
    OWNER_NOTES_HEADING,
    KnowledgeNote,
    format_notes_for_model,
    load_notes,
    note_file_text,
    note_slug,
    parse_note,
    save_note,
    select_notes,
    split_suggestion,
)

NOTE = "---\napplies_to: standing desk, Desk Frame\n---\nFrames with two motors hold their price.\n"


def test_a_note_reads_its_keywords_from_front_matter() -> None:
    note = parse_note("desks.md", NOTE)

    assert note == KnowledgeNote("desks.md", ("standing desk", "desk frame"), "Frames with two motors hold their price.")


def test_bracketed_and_dash_lists_both_work() -> None:
    assert parse_note("a.md", "---\napplies_to: [lamp, 'floor lamp']\n---\nx").applies_to == ("lamp", "floor lamp")
    dashed = parse_note("b.md", "---\ntitle: lamps\napplies_to:\n  - lamp\n  - shade\nother: 1\n---\nx")
    assert dashed.applies_to == ("lamp", "shade")


def test_a_note_without_front_matter_is_never_selected() -> None:
    note = parse_note("loose.md", "Just some text about desks.")

    assert note.applies_to == () and note.text == "Just some text about desks."
    assert select_notes([note], "a desk") == []


def test_notes_are_selected_by_whole_keywords_in_the_request_or_the_item_class() -> None:
    desks = parse_note("desks.md", NOTE)
    lamps = parse_note("lamps.md", "---\napplies_to: lamp\n---\nBulbs are sold separately.")

    assert select_notes([desks, lamps], "A cheap STANDING DESK please") == [desks]
    assert select_notes([desks, lamps], "office chairs", "a desk frame with lamp hooks") == [desks, lamps]
    assert select_notes([desks, lamps], "lampshades", None) == []


def test_the_model_sees_selected_notes_under_the_owners_heading() -> None:
    text = format_notes_for_model([parse_note("desks.md", NOTE)])

    assert text.startswith(OWNER_NOTES_HEADING)
    assert '<owner_note file="desks.md">\nFrames with two motors hold their price.\n</owner_note>' in text
    assert format_notes_for_model([]) == ""


def test_loading_reads_markdown_notes_but_not_the_readme(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("---\napplies_to: desk\n---\nThe format.")
    (tmp_path / "desks.md").write_text(NOTE)
    (tmp_path / "ignored.txt").write_text(NOTE)

    assert [note.file_name for note in load_notes(tmp_path)] == ["desks.md"]
    assert load_notes(tmp_path / "missing") == [] and load_notes(None) == []


def test_a_suggestion_keeps_its_own_keywords_or_falls_back_to_the_item_class() -> None:
    assert split_suggestion("applies_to: desk, frame\nTwo motors matter.", ["standing desks"]) == (
        ["desk", "frame"], "Two motors matter."
    )
    assert split_suggestion("Two motors matter.", ["Standing desks"]) == (["standing desks"], "Two motors matter.")


def test_slugs_and_file_text() -> None:
    assert note_slug("Second-hand Standing Desks (electric)!") == "second-hand-standing-desks-electric"
    assert note_slug("???") == "note"
    assert len(note_slug("word " * 40)) <= 60
    assert note_file_text(["desk", "frame"], "Body.\n") == "---\napplies_to: desk, frame\n---\nBody.\n"


def test_saving_never_overwrites_a_note(tmp_path: Path) -> None:
    first = save_note(tmp_path / "knowledge", "desks", "one")
    second = save_note(tmp_path / "knowledge", "desks", "two")

    assert (first.name, second.name) == ("desks.md", "desks-2.md")
    assert first.read_text() == "one" and second.read_text() == "two"
    assert load_notes(tmp_path / "knowledge")[0].file_name == "desks-2.md"
