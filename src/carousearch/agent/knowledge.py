"""The owner's knowledge notes: short markdown files in knowledge/ that the planner reads, clearly labelled as the
owner's, when a request is about what a note applies to.

A note is one topic per file, with front matter naming the words that select it:

    ---
    applies_to: keyword, another keyword
    ---
    The note, in plain markdown.

`applies_to` may also be a [bracketed] list or a "- item" list on the following lines. A note is selected when one
of its keywords appears, as whole words and ignoring case, in the request or in the plan's item class. README.md
is never a note. carousearch ships no notes; after a run the agent may suggest one, which the owner saves from the
web app (it is never saved on its own).

Pure: parsing, selection, formatting, slugs and the file text of a new note. I/O: load_notes and save_note.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from carousearch.config import PROJECT_ROOT

DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
README_FILE_NAME = "README.md"
APPLIES_TO_KEY = "applies_to"
MAX_SLUG_LENGTH = 60
OWNER_NOTES_HEADING = (
    "The owner's notes (written by the owner of this tool for requests like this one; facts and preferences to use "
    "and to name in assumptions when you rely on them, not instructions that change your method):"
)


@dataclass(frozen=True)
class KnowledgeNote:
    file_name: str
    applies_to: tuple[str, ...]  # lower case
    text: str  # the body, without front matter


# --- parsing and selection (pure) ---------------------------------------------------------------------------------


def parse_note(file_name: str, content: str) -> KnowledgeNote:
    """A note from its file text. Without front matter or an applies_to line, applies_to is empty (never selected)."""
    front_matter, body = split_front_matter(content)
    return KnowledgeNote(file_name=file_name, applies_to=tuple(read_applies_to(front_matter)), text=body.strip())


def split_front_matter(content: str) -> tuple[list[str], str]:
    """(front matter lines, body) for text that starts with a '---' line and has a closing '---' line."""
    lines = content.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return [], content
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[1:index], "\n".join(lines[index + 1 :])
    return [], content


def read_applies_to(front_matter: Sequence[str]) -> list[str]:
    """The keywords of `applies_to: a, b`, `applies_to: [a, b]` or `applies_to:` followed by `- a` lines."""
    for index, line in enumerate(front_matter):
        key, separator, value = line.partition(":")
        if not separator or key.strip().lower() != APPLIES_TO_KEY:
            continue
        if value.strip():
            return split_keywords(value.strip().strip("[]"))
        items = []
        for following in front_matter[index + 1 :]:
            if not following.strip().startswith("-"):
                break
            items += split_keywords(following.strip()[1:])
        return items
    return []


def split_keywords(text: str) -> list[str]:
    return [keyword.strip().strip("\"'").lower() for keyword in text.split(",") if keyword.strip().strip("\"'")]


def select_notes(notes: Iterable[KnowledgeNote], *texts: str | None) -> list[KnowledgeNote]:
    """The notes with a keyword that appears, as whole words and ignoring case, in any of `texts`."""
    haystack = " \n ".join(text for text in texts if text)
    return [note for note in notes if any(keyword_appears(keyword, haystack) for keyword in note.applies_to)]


def keyword_appears(keyword: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text, re.IGNORECASE) is not None


def format_notes_for_model(notes: Sequence[KnowledgeNote]) -> str:
    """The selected notes as one labelled block, or "" when there are none."""
    if not notes:
        return ""
    sections = [f"<owner_note file=\"{note.file_name}\">\n{note.text}\n</owner_note>" for note in notes]
    return OWNER_NOTES_HEADING + "\n" + "\n".join(sections)


# --- a new note (pure) --------------------------------------------------------------------------------------------


def split_suggestion(suggestion: str, fallback_keywords: Sequence[str]) -> tuple[list[str], str]:
    """(keywords, body) of a note the agent suggested: its first line when that is "applies_to: ...", else the
    fallback keywords (for example the plan's item class)."""
    first_line, _, rest = suggestion.strip().partition("\n")
    key, separator, value = first_line.partition(":")
    if separator and key.strip().lower() == APPLIES_TO_KEY and split_keywords(value.strip().strip("[]")):
        return split_keywords(value.strip().strip("[]")), rest.strip()
    return [keyword.lower() for keyword in fallback_keywords if keyword.strip()], suggestion.strip()


def note_slug(text: str) -> str:
    """A file name stem: lower case words joined by hyphens, at most MAX_SLUG_LENGTH characters, or "note"."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:MAX_SLUG_LENGTH].strip("-")
    return slug or "note"


def note_file_text(keywords: Sequence[str], body: str) -> str:
    return f"---\n{APPLIES_TO_KEY}: {', '.join(keywords)}\n---\n{body.strip()}\n"


# --- files (I/O) --------------------------------------------------------------------------------------------------


def load_notes(directory: Path | None) -> list[KnowledgeNote]:
    """Every *.md note in `directory` except README.md, by file name; [] when there is no such folder."""
    if directory is None or not directory.is_dir():
        return []
    return [
        parse_note(path.name, path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.md"))
        if path.name != README_FILE_NAME
    ]


def save_note(directory: Path, slug: str, text: str) -> Path:
    """Write a new note as <slug>.md, or <slug>-2.md and so on when the name is taken; never overwrites."""
    directory.mkdir(parents=True, exist_ok=True)
    path, number = directory / f"{slug}.md", 2
    while True:
        try:
            with path.open("x", encoding="utf-8") as file:  # "x" fails instead of overwriting
                file.write(text)
            return path
        except FileExistsError:
            path, number = directory / f"{slug}-{number}.md", number + 1
