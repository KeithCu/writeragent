# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Unit tests for transform_document_structure schema helpers."""

import json

from plugin.draw.transform_schema import (
    AUTOLAYOUT_BY_NAME,
    COLLABORA_TRANSFORM_DSL_URL,
    get_slide_commands,
    is_deferred_command_key,
    parse_transform_argument,
    resolve_layout_id,
)


def test_collabora_dsl_url_is_https():
    assert COLLABORA_TRANSFORM_DSL_URL.startswith("https://")
    assert "DocumentToolDescriptions.hpp" in COLLABORA_TRANSFORM_DSL_URL


def test_resolve_layout_autolayout_and_alias():
    assert resolve_layout_id("AUTOLAYOUT_TITLE") == 0
    assert resolve_layout_id("autolayout_title_content") == 1
    assert resolve_layout_id("title") == 0
    assert resolve_layout_id("blank") == 11
    assert resolve_layout_id(19) == 19


def test_resolve_layout_id_comprehensive():
    # Integers
    assert resolve_layout_id(0) == 0
    assert resolve_layout_id(19) == 19
    assert resolve_layout_id(-1) == -1

    # Floats with/without integer equivalence
    assert resolve_layout_id(19.0) == 19
    assert resolve_layout_id(0.0) == 0
    assert resolve_layout_id(19.5) is None
    assert resolve_layout_id(float("nan")) is None

    # Booleans (should return None because bool inherits from int in Python)
    assert resolve_layout_id(True) is None
    assert resolve_layout_id(False) is None

    # Non-string / non-numeric types
    assert resolve_layout_id(None) is None
    assert resolve_layout_id([]) is None
    assert resolve_layout_id({}) is None

    # Strings: empty or whitespace
    assert resolve_layout_id("") is None
    assert resolve_layout_id("   ") is None

    # Strings: AUTOLAYOUT names (case insensitivity and whitespace handling)
    assert resolve_layout_id("AUTOLAYOUT_TITLE") == 0
    assert resolve_layout_id("  autolayout_title_2content  ") == 3
    assert resolve_layout_id("autolayout_title_only") == 19

    # Strings: _LAYOUTS alias names
    assert resolve_layout_id("title") == 0
    assert resolve_layout_id("  BLANK  ") == 11
    assert resolve_layout_id("two_column_text") == 2

    # Strings: Numeric strings
    assert resolve_layout_id("19") == 19
    assert resolve_layout_id("  12  ") == 12
    assert resolve_layout_id("0") == 0
    assert resolve_layout_id("-5") == -5

    # Strings: Invalid / non-matching names or non-integer numbers
    assert resolve_layout_id("invalid_layout_name") is None
    assert resolve_layout_id("AUTOLAYOUT_NON_EXISTENT") is None
    assert resolve_layout_id("12.34") is None


def test_parse_transform_valid():
    payload = {"Transforms": {"SlideCommands": [{"JumpToSlide": 0}]}}
    obj, err = parse_transform_argument(json.dumps(payload))
    assert err is None
    assert obj == payload
    assert get_slide_commands(obj) == [{"JumpToSlide": 0}]


def test_parse_transform_dict_input():
    payload = {"Transforms": {"SlideCommands": []}}
    obj, err = parse_transform_argument(payload)
    assert err is None
    assert obj == payload


def test_parse_transform_invalid_json():
    obj, err = parse_transform_argument("{not json")
    assert obj is None
    assert err is not None
    assert "Invalid JSON" in err


def test_parse_transform_empty():
    obj, err = parse_transform_argument("")
    assert obj is None
    assert "No transform" in err


def test_deferred_keys():
    assert is_deferred_command_key("GenerateImage.1")
    assert is_deferred_command_key("ContentControls.ByIndex.0")
    assert is_deferred_command_key("MarkObject")
    assert not is_deferred_command_key("SetText.0")


def test_autolayout_map_matches_collabora_ids():
    assert AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_ONLY"] == 19
    assert AUTOLAYOUT_BY_NAME["AUTOLAYOUT_NONE"] == 20


def test_collabora_fixtures_load_from_tests_package():
    """Collabora payloads live under tests/, not plugin/ (see test_transform_collabora_fixtures.py)."""
    from tests.draw.collabora_transform_fixtures import COLLABORA_FIVE_SLIDE_TRANSFORM

    obj, err = parse_transform_argument(COLLABORA_FIVE_SLIDE_TRANSFORM)
    assert err is None
    assert len(get_slide_commands(obj)) == 31
