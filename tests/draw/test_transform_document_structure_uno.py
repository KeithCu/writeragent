# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""UNO tests for transform_document_structure on Impress."""

import json

from tests.draw.collabora_transform_fixtures import COLLABORA_FIVE_SLIDE_TRANSFORM_JSON

import unittest

from plugin.testing_runner import native_test, show_window
from plugin.tests.testing_utils import with_native_doc


def _exec_transform(doc, ctx, **args):
    from plugin.main import get_services
    from plugin.draw.transform import TransformDocumentStructure
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc, ctx, "impress", get_services(), "test")
    res = TransformDocumentStructure().execute(tctx, **args)
    return json.dumps(res) if isinstance(res, dict) else res


@native_test
@with_native_doc("impress")
def test_transform_layout_and_set_text(ctx, doc):
    transform = json.dumps(
        {
            "Transforms": {
                "SlideCommands": [
                    {"ChangeLayoutByName": "AUTOLAYOUT_TITLE"},
                    {"SetText.0": "Transform Test Title"},
                    {"SetText.1": "Subtitle line"},
                ]
            }
        }
    )
    result = _exec_transform(doc, ctx, transform=transform)
    data = json.loads(result)
    assert data.get("status") == "ok", result
    assert "ChangeLayoutByName" in "".join(data.get("applied", [])) or "SetText" in "".join(data.get("applied", []))


@native_test
@with_native_doc("impress")
def test_transform_insert_second_slide(ctx, doc):
    transform = json.dumps(
        {
            "Transforms": {
                "SlideCommands": [
                    {"InsertMasterSlide": 0},
                    {"ChangeLayoutByName": "AUTOLAYOUT_TITLE_CONTENT"},
                    {"SetText.0": "Slide Two"},
                    {"JumpToSlide": 0},
                ]
            }
        }
    )
    result = _exec_transform(doc, ctx, transform=transform)
    data = json.loads(result)
    assert data.get("status") == "ok", result
    pages = doc.getDrawPages()
    assert pages.getCount() >= 2, "expected at least two slides after InsertMasterSlide"


@unittest.skipIf(not show_window, "Collabora five slide documentation example has transient errors under narrow process layout limitations")
@native_test
@with_native_doc("impress", hidden=not show_window)
def test_collabora_five_slide_documentation_example(ctx, doc):
    """Full 5-slide deck from Collabora DocumentToolDescriptions.hpp (integration smoke)."""
    result = _exec_transform(doc, ctx, transform=COLLABORA_FIVE_SLIDE_TRANSFORM_JSON)
    data = json.loads(result)
    assert data.get("status") == "ok", result
    pages = doc.getDrawPages()
    assert pages.getCount() >= 5, "Collabora example inserts 4 slides after the first"
    applied = data.get("applied") or []
    assert count_applied_prefix(applied, "InsertMasterSlide") >= 4
    assert count_applied_prefix(applied, "SetText.") >= 5


def count_applied_prefix(applied: list[str], prefix: str) -> int:
    return sum(1 for entry in applied if prefix in entry)
