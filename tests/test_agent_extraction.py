"""extract_fields' pure parts: field specs, typing, regexes, the worker prompt and answer shape, cache keys."""

import pytest
from pydantic import ValidationError

from carousearch.agent.extraction import (
    FieldSpec,
    apply_pattern,
    build_extraction_prompt,
    coerce_value,
    column_name,
    column_type,
    extraction_answer_model,
    listing_source_text,
    listing_text_block,
    spec_cache_key,
)


def spec(type_: str = "int", **changes) -> FieldSpec:
    fields = {"name": "size_cm", "type": type_, "hint": "the width in centimetres"} | changes
    return FieldSpec(**fields)


def test_a_spec_needs_a_valid_name_a_compiling_pattern_and_values_for_an_enum() -> None:
    with pytest.raises(ValidationError):
        spec(name="Size")
    with pytest.raises(ValidationError, match="not a valid regex"):
        spec(pattern="(unclosed")
    with pytest.raises(ValidationError, match="needs enum_values"):
        spec("enum")
    with pytest.raises(ValidationError, match="reserved"):
        spec(name="listing_id")


@pytest.mark.parametrize(
    ("type_", "raw", "expected"),
    [
        ("int", "1,200 cm", 1200), ("int", 140.0, 140), ("int", "2.5", None), ("int", True, None),
        ("float", "about 2.5 kg", 2.5), ("float", "none", None), ("float", "", None),
        ("bool", "Yes", 1), ("bool", False, 0), ("bool", "maybe", None),
        ("date", "Mar 2028", "2028-03-01"), ("date", "2027-11-05", "2027-11-05"), ("date", "05/11/2027", "2027-11-05"),
        ("date", "next year", None),
        ("str", "  two   words ", "two words"), ("str", "unknown", None), ("str", None, None),
    ],
)
def test_values_are_typed_or_null_never_guessed(type_: str, raw: object, expected: object) -> None:
    assert coerce_value(spec(type_), raw) == expected


def test_enum_values_match_ignoring_case_and_anything_else_is_null() -> None:
    colours = spec("enum", name="colour", enum_values=["Black", "Oak"])
    assert coerce_value(colours, "oak") == "Oak"
    assert coerce_value(colours, "walnut") is None


def test_a_pattern_returns_its_first_group_typed_or_the_whole_match() -> None:
    assert apply_pattern(spec(pattern=r"(\d+)\s*cm"), "Desk 140CM wide") == 140
    assert apply_pattern(spec("str", name="code", pattern=r"[A-Z]{2}\d{3}"), "model ab123 here") == "ab123"
    assert apply_pattern(spec(pattern=r"(\d+)\s*cm"), "no size given") is None
    assert apply_pattern(spec(), "140 cm") is None  # no pattern: the model decides


def test_columns_are_prefixed_and_typed_for_sqlite() -> None:
    assert (column_name(spec()), column_type(spec())) == ("x_size_cm", "INTEGER")
    assert column_type(spec("bool")) == "INTEGER" and column_type(spec("float")) == "REAL"
    assert column_type(spec("date")) == "TEXT" and column_type(spec("enum", enum_values=["a"])) == "TEXT"


def test_the_cache_key_changes_with_the_spec_and_the_text_read() -> None:
    key = spec_cache_key(spec(), "description")
    assert key.startswith("extract:size_cm:") and key.endswith(":description")
    assert key == spec_cache_key(spec(), "description")
    assert key != spec_cache_key(spec(hint="the depth in centimetres"), "description")
    assert key != spec_cache_key(spec(), "snippet")


def test_the_best_text_is_the_description_then_the_snippet_then_nothing() -> None:
    assert listing_source_text({"description": "full", "snippet": "short"}) == ("full", "description")
    assert listing_source_text({"description": None, "snippet": "short"}) == ("short", "snippet")
    assert listing_source_text({"description": "", "snippet": None}) == (None, "title")


def test_the_prompt_lists_the_fields_then_each_listing() -> None:
    colours = spec("enum", name="colour", hint="the frame colour", enum_values=["Black", "Oak"])
    prompt = build_extraction_prompt(
        [spec(unit="cm"), colours], [listing_text_block("7", "Desk", "Oak, 140cm"), listing_text_block("8", "T", None)]
    )

    assert prompt.startswith("Fields to fill for every listing:\n- size_cm (int, in cm): the width in centimetres\n")
    assert "- colour (enum): the frame colour Allowed values: Black, Oak." in prompt
    assert '<listing id="7">\nTitle: Desk\nText:\nOak, 140cm\n</listing>' in prompt
    assert "(no description available)" in prompt


def test_the_answer_model_has_one_nullable_typed_field_per_spec() -> None:
    colours = spec("enum", name="colour", enum_values=["Black", "Oak"])
    answer_model = extraction_answer_model([spec(), colours])

    parsed = answer_model.model_validate({"rows": [{"listing_id": "7", "size_cm": 140, "colour": None}]})
    assert parsed.rows[0].size_cm == 140 and parsed.rows[0].colour is None
    with pytest.raises(ValidationError):
        answer_model.model_validate({"rows": [{"listing_id": "7", "size_cm": 1, "colour": "Walnut"}]})
    schema = answer_model.model_json_schema()
    row_schema = schema["$defs"]["ExtractedRow"]
    assert set(row_schema["required"]) == {"listing_id", "size_cm", "colour"}  # strict mode: all present, nullable
