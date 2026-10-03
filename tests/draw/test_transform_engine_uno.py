# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Slide current-page and EditTextObject selection against a live Impress doc."""

import json

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _exec_transform(doc, ctx, transform):
    from plugin.main import get_services
    from plugin.draw.transform import TransformDocumentStructure
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc, ctx, "impress", get_services(), "test")
    res = TransformDocumentStructure().execute(tctx, transform=json.dumps(transform))
    return res if isinstance(res, dict) else json.loads(res)


def _names(doc):
    pages = doc.getDrawPages()
    return [pages.getByIndex(i).Name for i in range(pages.getCount())]


def _first_text_shape(page):
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if hasattr(shape, "getText"):
            return shape
    raise AssertionError("no text shape")


def _char_weights(shape):
    text = shape.getText()
    raw = text.getString()
    weights = []
    for i in range(len(raw)):
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        if i:
            cursor.goRight(i, False)
        cursor.goRight(1, True)
        weights.append(float(cursor.CharWeight))
    return raw, weights


def _is_italic(shape, index):
    text = shape.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    if index:
        cursor.goRight(index, False)
    cursor.goRight(1, True)
    return "ITALIC" in str(cursor.CharPosture).upper()


@native_test
@with_native_doc("impress")
def test_move_other_slide_keeps_current_on_the_same_page(ctx, doc):
    # Four slides. Current is S2. Move slide 0 to index 3. S2 shifts to index 1.
    commands = [{"InsertMasterSlide": 0}, {"InsertMasterSlide": 0}, {"InsertMasterSlide": 0}]
    for index, name in enumerate(("S0", "S1", "S2", "S3")):
        commands.append({"JumpToSlide": index})
        commands.append({"RenameSlide": name})
    commands.append({"JumpToSlide": 2})
    commands.append({"MoveSlide.0": 3})
    commands.append({"RenameSlide": "HERE"})
    result = _exec_transform(doc, ctx, {"Transforms": {"SlideCommands": commands}})
    assert result.get("status") == "ok", result
    assert result.get("current_slide") == 1, result
    assert _names(doc)[1] == "HERE"
    assert _names(doc)[3] == "S0"


@native_test
@with_native_doc("impress")
def test_delete_earlier_slide_decrements_current(ctx, doc):
    commands = [{"InsertMasterSlide": 0}, {"InsertMasterSlide": 0}, {"InsertMasterSlide": 0}]
    for index, name in enumerate(("S0", "S1", "S2", "S3")):
        commands.append({"JumpToSlide": index})
        commands.append({"RenameSlide": name})
    commands.append({"JumpToSlide": 2})
    commands.append({"DeleteSlide": 0})
    commands.append({"RenameSlide": "HERE"})
    result = _exec_transform(doc, ctx, {"Transforms": {"SlideCommands": commands}})
    assert result.get("status") == "ok", result
    assert result.get("current_slide") == 1, result
    # Former S2 slid into index 1. Staying at 2 would rename former S3.
    assert _names(doc) == ["S1", "HERE", "S3"]


@native_test
@with_native_doc("impress")
def test_edit_text_object_bold_applies_to_the_selection(ctx, doc):
    result = _exec_transform(
        doc,
        ctx,
        {
            "Transforms": {
                "SlideCommands": [
                    {"ChangeLayoutByName": "AUTOLAYOUT_TITLE"},
                    {"SetText.0": "HelloWorld"},
                    {"EditTextObject.0": [{"SelectText": [0, 0, 0, 4]}, {"UnoCommand": ".uno:Bold"}]},
                ]
            }
        },
    )
    assert result.get("status") == "ok", result
    assert not any("does not apply" in w for w in result.get("warnings") or []), result
    raw, weights = _char_weights(_first_text_shape(doc.getDrawPages().getByIndex(0)))
    assert raw == "HelloWorld"
    assert weights[:4] == [150.0, 150.0, 150.0, 150.0], weights
    assert weights[4:] == [100.0, 100.0, 100.0, 100.0, 100.0, 100.0], weights


@native_test
@with_native_doc("impress")
def test_insert_text_then_italic_formats_only_the_insertion(ctx, doc):
    result = _exec_transform(
        doc,
        ctx,
        {
            "Transforms": {
                "SlideCommands": [
                    {"ChangeLayoutByName": "AUTOLAYOUT_TITLE"},
                    {"SetText.0": "Hello\nWorld"},
                    {
                        "EditTextObject.0": [
                            {"SelectParagraph": 0},
                            {"InsertText": "Hi"},
                            {"UnoCommand": ".uno:Italic"},
                        ]
                    },
                ]
            }
        },
    )
    assert result.get("status") == "ok", result
    shape = _first_text_shape(doc.getDrawPages().getByIndex(0))
    assert shape.getText().getString() == "Hi\nWorld"
    assert _is_italic(shape, 0) is True
    assert _is_italic(shape, 1) is True
    assert _is_italic(shape, 3) is False
