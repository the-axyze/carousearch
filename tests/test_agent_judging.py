"""The judge tools' prompts, answer completion and batching (pure)."""

from pydantic_ai.messages import BinaryContent

from carousearch.agent.judging import (
    NO_VERDICT_REASON,
    ListingText,
    ListingVerdict,
    PhotoScore,
    batched,
    build_listing_prompt,
    build_photo_prompt,
    complete_scores,
    complete_verdicts,
    cut_description,
    normalise_criterion,
)
from carousearch.photos import Photo


def test_the_listing_prompt_has_the_criterion_then_one_block_per_listing() -> None:
    prompt = build_listing_prompt(
        "COE about 2 years",
        [ListingText("1", "R15 V3", 2500.0, "COE till 2028"), ListingText("2", "MT15", None, None)],
    )

    assert prompt.startswith("Criterion: COE about 2 years\n\n")
    assert '<listing id="1">\nTitle: R15 V3\nPrice: S$2,500\nDescription:\nCOE till 2028\n</listing>' in prompt
    assert "Price: not a plain number" in prompt and "(no description available)" in prompt


def test_long_descriptions_are_cut_for_the_judge() -> None:
    assert cut_description("a" * 2000).endswith(" [cut]") and len(cut_description("a" * 2000)) == 1206


def test_the_photo_prompt_puts_each_photo_after_its_listing_id() -> None:
    content = build_photo_prompt("blurry", [("7", Photo(data=b"jpeg-bytes", format="jpeg"))])

    assert content[0].startswith("Criterion: blurry")
    assert content[1] == "Listing 7:"
    assert isinstance(content[2], BinaryContent) and content[2].media_type == "image/jpeg"


def test_missing_verdicts_are_filled_in_as_unsure_and_extras_dropped() -> None:
    returned = [
        ListingVerdict(listing_id="2", verdict="no", reason="COE 2031"),
        ListingVerdict(listing_id="9", verdict="yes", reason="not asked"),
    ]

    verdicts = complete_verdicts(["1", "2"], returned)

    assert [(verdict.listing_id, verdict.verdict) for verdict in verdicts] == [("1", "unsure"), ("2", "no")]
    assert verdicts[0].reason == NO_VERDICT_REASON


def test_missing_scores_are_none() -> None:
    scores = complete_scores(["1", "2"], [PhotoScore(listing_id="1", score=4, reason="dim")])

    assert scores["1"] == PhotoScore(listing_id="1", score=4, reason="dim") and scores["2"] is None


def test_batches_and_criteria() -> None:
    assert batched([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert batched([], 3) == []
    assert normalise_criterion("  Blurry   OR low-effort\nphoto ") == "blurry or low-effort photo"

