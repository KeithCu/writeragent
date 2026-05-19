# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Tests using Collabora Online transform payloads and documentation examples."""

import json

import pytest

from tests.draw.collabora_transform_fixtures import (
    COLLABORA_FIVE_SLIDE_TRANSFORM,
    COLLABORA_FIVE_SLIDE_TRANSFORM_JSON,
    COLLABORA_INSERT_IMAGE_AT_MINI,
    COLLABORA_INVALID_TRANSFORM_ERROR_FRAGMENT,
    COLLABORA_NAVIGATION_ONLY_TRANSFORM,
    COLLABORA_WRITER_CONTENT_CONTROL_TRANSFORM,
    COLLABORA_WRITER_CONTENT_CONTROL_TRANSFORM_JSON,
    count_slide_commands_key_prefix,
    count_slide_commands_with_key,
    slide_command_key_sets,
)
from plugin.draw.transform_schema import get_slide_commands, is_deferred_command_key, parse_transform_argument


def test_collabora_five_slide_example_parses():
    obj, err = parse_transform_argument(COLLABORA_FIVE_SLIDE_TRANSFORM_JSON)
    assert err is None
    assert obj == COLLABORA_FIVE_SLIDE_TRANSFORM


def test_collabora_five_slide_example_inventory():
    """Command counts from DocumentToolDescriptions.hpp 5-slide deck example."""
    cmds = get_slide_commands(COLLABORA_FIVE_SLIDE_TRANSFORM)
    assert len(cmds) == 31
    assert count_slide_commands_with_key(cmds, "InsertMasterSlide") == 4
    assert count_slide_commands_with_key(cmds, "ChangeLayoutByName") == 5
    assert count_slide_commands_key_prefix(cmds, "SetText.") == 10
    assert count_slide_commands_key_prefix(cmds, "EditTextObject.") == 10
    assert count_slide_commands_with_key(cmds, "RenameSlide") == 1
    assert count_slide_commands_with_key(cmds, "JumpToSlide") == 1
    key_sets = slide_command_key_sets(cmds)
    assert all(len(ks) >= 1 for ks in key_sets)


def test_collabora_five_slide_first_slide_ops():
    cmds = get_slide_commands(COLLABORA_FIVE_SLIDE_TRANSFORM)
    assert cmds[0] == {"ChangeLayoutByName": "AUTOLAYOUT_TITLE"}
    assert cmds[1] == {"SetText.0": "Quarterly Report"}
    assert cmds[3] == {"RenameSlide": "Title"}


def test_collabora_writer_content_control_parses():
    obj, err = parse_transform_argument(COLLABORA_WRITER_CONTENT_CONTROL_TRANSFORM_JSON)
    assert err is None
    assert obj == COLLABORA_WRITER_CONTENT_CONTROL_TRANSFORM
    # WriterAgent V1 engine only runs SlideCommands; content controls are separate keys.
    assert get_slide_commands(obj) == []


def test_collabora_writer_content_control_keys_are_deferred():
    assert is_deferred_command_key("ContentControls.ByIndex.0")


def test_collabora_navigation_only_parses():
    obj, err = parse_transform_argument(COLLABORA_NAVIGATION_ONLY_TRANSFORM)
    assert err is None
    cmds = get_slide_commands(obj)
    assert len(cmds) == 2
    assert all("JumpToSlide" in cmd for cmd in cmds)


def test_collabora_invalid_json_matches_aichat_error_text():
    obj, err = parse_transform_argument('{"Transforms":{"SlideCommands":[invalid}')
    assert obj is None
    assert err is not None
    assert COLLABORA_INVALID_TRANSFORM_ERROR_FRAGMENT in err


def test_collabora_insert_image_at_key_deferred():
    cmds = get_slide_commands(COLLABORA_INSERT_IMAGE_AT_MINI)
    assert len(cmds) == 1
    assert is_deferred_command_key(next(iter(cmds[0].keys())))


@pytest.mark.parametrize(
    "payload_json",
    [
        COLLABORA_FIVE_SLIDE_TRANSFORM_JSON,
        COLLABORA_WRITER_CONTENT_CONTROL_TRANSFORM_JSON,
        json.dumps(COLLABORA_NAVIGATION_ONLY_TRANSFORM),
    ],
)
def test_collabora_fixtures_roundtrip_json(payload_json: str):
    obj1, err = parse_transform_argument(payload_json)
    assert err is None
    obj2, err2 = parse_transform_argument(json.dumps(obj1))
    assert err2 is None
    assert obj2 == obj1
