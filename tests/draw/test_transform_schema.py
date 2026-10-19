# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Unit tests for transform_document_structure schema helpers."""

import json

from plugin.draw.transform_schema import (
    AUTOLAYOUT_BY_NAME,
    AUTOLAYOUT_ID,
    COLLABORA_TRANSFORM_DSL_URL,
    get_slide_commands,
    is_deferred_command_key,
    parse_transform_argument,
    resolve_layout_id,
    TRANSFORM_PARAM_DESCRIPTION,
)


def test_collabora_dsl_url_is_https():
    assert COLLABORA_TRANSFORM_DSL_URL.startswith("https://")
    assert "DocumentToolDescriptions.hpp" in COLLABORA_TRANSFORM_DSL_URL


def test_resolve_layout_autolayout_and_alias():
    assert resolve_layout_id("AUTOLAYOUT_TITLE") == 0
    assert resolve_layout_id("autolayout_title_content") == 1
    assert resolve_layout_id("AUTOLAYOUT_NONE") == 20
    assert resolve_layout_id("blank") is None
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

    # Transform DSL names are AUTOLAYOUT_*; friendly names go through layout_id.
    assert resolve_layout_id("title") is None
    assert resolve_layout_id("  BLANK  ") is None
    assert resolve_layout_id("two_column_text") is None

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
    assert err is not None and "No transform" in err


def test_deferred_keys():
    assert is_deferred_command_key("GenerateImage.1")
    assert is_deferred_command_key("ContentControls.ByIndex.0")
    assert is_deferred_command_key("MarkObject")
    assert not is_deferred_command_key("SetText.0")


def test_autolayout_map_matches_collabora_ids():
    assert AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_ONLY"] == 19
    assert AUTOLAYOUT_BY_NAME["AUTOLAYOUT_NONE"] == 20
    assert AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_2CONTENT"] == AUTOLAYOUT_ID["AUTOLAYOUT_TITLE_2CONTENT"] == 3
    # Historic ids the Collabora subset omits. set_slide_layout still uses them.
    assert AUTOLAYOUT_ID["AUTOLAYOUT_CHART"] == 2
    assert AUTOLAYOUT_ID["AUTOLAYOUT_TAB"] == 8
    assert AUTOLAYOUT_ID["AUTOLAYOUT_OBJ"] == 11
    assert AUTOLAYOUT_ID["AUTOLAYOUT_TITLE_ONLY"] == AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_ONLY"]


def test_collabora_fixtures_load_from_tests_package():
    """Collabora payloads live under tests/, not plugin/ (see test_transform_collabora_fixtures.py)."""
    from tests.draw.collabora_transform_fixtures import COLLABORA_FIVE_SLIDE_TRANSFORM

    obj, err = parse_transform_argument(COLLABORA_FIVE_SLIDE_TRANSFORM)
    assert err is None
    assert obj is not None
    assert len(get_slide_commands(obj)) == 31


def test_all_autolayout_by_name_appear_in_transform_description():
    for name, layout_id in AUTOLAYOUT_BY_NAME.items():
        expected = f"{name} (id={layout_id})"
        assert expected in TRANSFORM_PARAM_DESCRIPTION


def test_classify_command_key():
    from plugin.draw.transform_schema import classify_command_key

    assert classify_command_key("JumpToSlide") == "JumpToSlide"
    assert classify_command_key("SetText.0") == "SetText"
    assert classify_command_key("EditTextObject.2") == "EditTextObject"
    assert classify_command_key("MoveSlide.1") == "MoveSlide"
    assert classify_command_key("GenerateImage.0") == "deferred"
    assert classify_command_key("MarkObject") == "deferred"
    assert classify_command_key("NoSuchCommand") is None
    assert classify_command_key(123) is None


def test_validate_slide_commands_success_and_missing():
    from plugin.draw.transform_schema import validate_slide_commands

    assert validate_slide_commands({}) == (None, None)
    assert validate_slide_commands({"Transforms": {}}) == (None, None)
    assert validate_slide_commands({"Transforms": {"SlideCommands": []}}) == (None, None)
    valid_payload = {
        "Transforms": {
            "SlideCommands": [
                {"JumpToSlide": 0},
                {"ChangeLayoutByName": "AUTOLAYOUT_TITLE"},
                {"SetText.0": "Hello"},
                {
                    "EditTextObject.0": [
                        {"SelectText": []},
                        {"UnoCommand": ".uno:Bold"},
                        {"SelectParagraph": 0},
                        {"InsertText": "World"},
                    ]
                },
            ]
        }
    }
    assert validate_slide_commands(valid_payload) == (None, None)


def test_validate_slide_commands_non_dict_and_empty_and_multikey():
    from plugin.draw.transform_schema import validate_slide_commands

    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [42]}})
    assert err is not None and "non-empty object with one key" in err
    assert idx == 0

    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"JumpToSlide": 0}, {}]}})
    assert err is not None and "non-empty object with one key" in err
    assert idx == 1

    err, idx = validate_slide_commands(
        {"Transforms": {"SlideCommands": [{"JumpToSlide": 0, "DeleteSlide": 1}]}}
    )
    assert err is not None and "exactly one command key" in err
    assert idx == 0


def test_validate_slide_commands_deferred_and_unknown():
    from plugin.draw.transform_schema import validate_slide_commands

    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"GenerateImage.0": {}}]}})
    assert err is not None and "GenerateImage.0 is not supported" in err
    assert idx == 0

    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"NoSuchCommand": 1}]}})
    assert err is not None and "Unknown or unsupported command key: NoSuchCommand" in err
    assert idx == 0


def test_validate_slide_commands_shape_checks():
    from plugin.draw.transform_schema import validate_slide_commands

    # Bool or non-int MoveSlide
    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"MoveSlide": True}]}})
    assert err is not None and "Invalid MoveSlide index" in err
    assert idx == 0

    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"MoveSlide.0": "not-int"}]}})
    assert err is not None and "Invalid MoveSlide.0 index" in err
    assert idx == 0

    # Invalid layout
    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"ChangeLayout": 9999}]}})
    assert err is not None and "Unknown layout in ChangeLayout" in err
    assert idx == 0

    # Non-string SetText
    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"SetText.0": 123}]}})
    assert err is not None and "SetText.0 requires a string" in err
    assert idx == 0

    # Non-string UnoCommand
    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"UnoCommand": 123}]}})
    assert err is not None and "UnoCommand requires a string" in err
    assert idx == 0


def test_validate_slide_commands_edit_text_object():
    from plugin.draw.transform_schema import validate_slide_commands

    # Empty array
    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"EditTextObject.0": []}]}})
    assert err is not None and "requires an array of sub-commands" in err
    assert idx == 0

    # Sub-command not dict
    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"EditTextObject.0": [42]}]}})
    assert err is not None and "sub-command must be an object with one key" in err
    assert idx == 0

    # Sub-command empty dict
    err, idx = validate_slide_commands({"Transforms": {"SlideCommands": [{"EditTextObject.0": [{}]}]}})
    assert err is not None and "sub-command must be an object with one key" in err
    assert idx == 0

    # SelectText length > 4
    err, idx = validate_slide_commands(
        {"Transforms": {"SlideCommands": [{"EditTextObject.0": [{"SelectText": [1, 2, 3, 4, 5]}]}]}}
    )
    assert err is not None and "0 to 4 integers" in err
    assert idx == 0

    # SelectText negatives and non-int
    err, idx = validate_slide_commands(
        {"Transforms": {"SlideCommands": [{"EditTextObject.0": [{"SelectText": [-1]}]}]}}
    )
    assert err is not None and "negative paragraph index" in err
    assert idx == 0

    err, idx = validate_slide_commands(
        {"Transforms": {"SlideCommands": [{"EditTextObject.0": [{"SelectText": [True]}]}]}}
    )
    assert err is not None and "Invalid non-int element" in err
    assert idx == 0

    # SelectText None
    err, idx = validate_slide_commands(
        {"Transforms": {"SlideCommands": [{"EditTextObject.0": [{"SelectText": None}]}]}}
    )
    assert err is not None and "requires a list" in err
    assert idx == 0

    # SelectParagraph non-int or negative
    err, idx = validate_slide_commands(
        {"Transforms": {"SlideCommands": [{"EditTextObject.0": [{"SelectParagraph": True}]}]}}
    )
    assert err is not None and "Invalid SelectParagraph index" in err
    assert idx == 0

    err, idx = validate_slide_commands(
        {"Transforms": {"SlideCommands": [{"EditTextObject.0": [{"SelectParagraph": -1}]}]}}
    )
    assert err is not None and "negative paragraph index" in err
    assert idx == 0

    # InsertText non-string
    err, idx = validate_slide_commands(
        {"Transforms": {"SlideCommands": [{"EditTextObject.0": [{"InsertText": 123}]}]}}
    )
    assert err is not None and "InsertText requires a string" in err
    assert idx == 0


