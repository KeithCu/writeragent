# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Collabora Online test payloads and documentation examples (ported for WriterAgent tests).
#
# Sources:
#   - Impress 5-slide example: wsd/DocumentToolDescriptions.hpp (TRANSFORM_PARAM_DESCRIPTION)
#     https://github.com/CollaboraOnline/online/blob/master/wsd/DocumentToolDescriptions.hpp
#   - Writer content control: test/integration-http-server.cpp (HTTPServerTest::testTransformDocStructure)
#     https://github.com/CollaboraOnline/online/blob/master/test/integration-http-server.cpp
#   - Navigation-only approval pattern: wsd/AIChatSession.cpp (~960-980)
#     https://github.com/CollaboraOnline/online/blob/master/wsd/AIChatSession.cpp

from __future__ import annotations

import json
from typing import Any

# Full example from DocumentToolDescriptions.hpp (line ~131).
COLLABORA_FIVE_SLIDE_TRANSFORM_JSON = (
    '{"Transforms":{"SlideCommands":['
    '{"ChangeLayoutByName":"AUTOLAYOUT_TITLE"},'
    '{"SetText.0":"Quarterly Report"},'
    '{"SetText.1":"Q1 2026"},'
    '{"RenameSlide":"Title"},'
    '{"EditTextObject.0":[{"SelectText":[]},{"UnoCommand":".uno:Bold"},{"UnoCommand":".uno:CenterPara"}]},'
    '{"InsertMasterSlide":0},'
    '{"ChangeLayoutByName":"AUTOLAYOUT_TITLE_CONTENT"},'
    '{"SetText.0":"Revenue"},'
    '{"SetText.1":"Revenue grew 15% year over year\\nNew markets contributed 30% of growth\\nCustomer retention at 95%"},'
    '{"EditTextObject.0":[{"SelectText":[]},{"UnoCommand":".uno:Bold"}]},'
    '{"EditTextObject.1":[{"SelectText":[]},{"UnoCommand":".uno:DefaultBullet"}]},'
    '{"InsertMasterSlide":0},'
    '{"ChangeLayoutByName":"AUTOLAYOUT_TITLE_2CONTENT"},'
    '{"SetText.0":"Strengths & Risks"},'
    '{"SetText.1":"Strong brand recognition\\nGrowing user base\\nHigh retention rate"},'
    '{"SetText.2":"Supply chain delays\\nRegulatory changes\\nCompetitor pricing"},'
    '{"EditTextObject.0":[{"SelectText":[]},{"UnoCommand":".uno:Bold"}]},'
    '{"EditTextObject.1":[{"SelectText":[]},{"UnoCommand":".uno:DefaultBullet"}]},'
    '{"EditTextObject.2":[{"SelectText":[]},{"UnoCommand":".uno:DefaultBullet"}]},'
    '{"InsertMasterSlide":0},'
    '{"ChangeLayoutByName":"AUTOLAYOUT_TITLE_CONTENT"},'
    '{"SetText.0":"Roadmap"},'
    '{"SetText.1":"Phase 1: Research\\nPhase 2: Development\\nPhase 3: Launch"},'
    '{"EditTextObject.0":[{"SelectText":[]},{"UnoCommand":".uno:Bold"}]},'
    '{"EditTextObject.1":[{"SelectText":[]},{"UnoCommand":".uno:DefaultNumbering"},'
    '{"SelectParagraph":0},{"UnoCommand":".uno:Bold"}]},'
    '{"InsertMasterSlide":0},'
    '{"ChangeLayoutByName":"AUTOLAYOUT_TITLE_ONLY"},'
    '{"SetText.0":"Thank You"},'
    '{"EditTextObject.0":[{"SelectText":[]},{"UnoCommand":".uno:Bold"},{"UnoCommand":".uno:CenterPara"}]},'
    '{"JumpToSlide":1},'
    '{"EditTextObject.1":[{"SelectParagraph":0},{"InsertText":"Revenue grew 15% YoY"},'
    '{"UnoCommand":".uno:Bold"},{"UnoCommand":".uno:Italic"}]}'
    "]}}"
)

COLLABORA_FIVE_SLIDE_TRANSFORM: dict[str, Any] = json.loads(COLLABORA_FIVE_SLIDE_TRANSFORM_JSON)

# integration-http-server.cpp testTransformDocStructure POST field "transform".
COLLABORA_WRITER_CONTENT_CONTROL_TRANSFORM_JSON = (
    '{"Transforms":{"ContentControls.ByIndex.0":{"content":"Short text"}}}'
)
COLLABORA_WRITER_CONTENT_CONTROL_TRANSFORM: dict[str, Any] = json.loads(COLLABORA_WRITER_CONTENT_CONTROL_TRANSFORM_JSON)

# Expected extract JSON snippet after transform (same test file, testExtractDocStructure check).
COLLABORA_WRITER_CONTENT_CONTROL_EXPECTED_CONTENT = "Short text"

# AIChatSession navigation-only fast path (mutating ops would disable this in Collabora).
COLLABORA_NAVIGATION_ONLY_TRANSFORM_JSON = (
    '{"Transforms":{"SlideCommands":[{"JumpToSlide":0},{"JumpToSlide":1}]}}'
)
COLLABORA_NAVIGATION_ONLY_TRANSFORM: dict[str, Any] = json.loads(COLLABORA_NAVIGATION_ONLY_TRANSFORM_JSON)

# AIChatSession.cpp GenerateImage follow-up mini-transform shape (InsertImageAt; V1 deferred).
COLLABORA_INSERT_IMAGE_AT_MINI_JSON = (
    '{"Transforms":{"SlideCommands":[{"InsertImageAt.0.1":"file:///tmp/example.png"}]}}'
)
COLLABORA_INSERT_IMAGE_AT_MINI: dict[str, Any] = json.loads(COLLABORA_INSERT_IMAGE_AT_MINI_JSON)

# Error text fragment from AIChatSession.cpp when transform JSON fails to parse.
COLLABORA_INVALID_TRANSFORM_ERROR_FRAGMENT = "Invalid JSON in transform parameter"


def slide_command_key_sets(commands: list[dict[str, Any]]) -> list[frozenset[str]]:
    """Return the key set for each SlideCommands entry (Collabora uses one op per object)."""
    return [frozenset(cmd.keys()) for cmd in commands]


def count_slide_commands_with_key(commands: list[dict[str, Any]], key: str) -> int:
    return sum(1 for cmd in commands if key in cmd)


def count_slide_commands_key_prefix(commands: list[dict[str, Any]], prefix: str) -> int:
    return sum(1 for cmd in commands for k in cmd if k.startswith(prefix))
