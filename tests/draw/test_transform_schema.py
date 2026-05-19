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
