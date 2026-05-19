# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""UNO tests for transform_document_structure on Impress."""

import json

from tests.draw.collabora_transform_fixtures import COLLABORA_FIVE_SLIDE_TRANSFORM_JSON
from plugin.framework.logging import log
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import native_test, setup, teardown

_test_doc = None
_test_ctx = None


@setup
def setup_transform_impress_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno

    hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    _test_doc = desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, (hidden,))
    assert _test_doc is not None
    log.info("[TransformTests] impress document loaded")


@teardown
def teardown_transform_impress_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


def _exec_transform(**args):
    from plugin.main import get_services
    from plugin.draw.transform import TransformDocumentStructure
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(_test_doc, _test_ctx, "impress", get_services(), "test")
    res = TransformDocumentStructure().execute(tctx, **args)
    return json.dumps(res) if isinstance(res, dict) else res


@native_test
def test_transform_layout_and_set_text():
    try:
        import pytest

        if _test_doc is None:
            pytest.skip("Requires LibreOffice from native runner")
    except ImportError:
        pass

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
    result = _exec_transform(transform=transform)
    data = json.loads(result)
    assert data.get("status") == "ok", result
    assert "ChangeLayoutByName" in "".join(data.get("applied", [])) or "SetText" in "".join(data.get("applied", []))


@native_test
def test_transform_insert_second_slide():
    try:
        import pytest

        if _test_doc is None:
            pytest.skip("Requires LibreOffice from native runner")
    except ImportError:
        pass

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
    result = _exec_transform(transform=transform)
    data = json.loads(result)
    assert data.get("status") == "ok", result
    pages = _test_doc.getDrawPages()
    assert pages.getCount() >= 2, "expected at least two slides after InsertMasterSlide"


@native_test
def test_collabora_five_slide_documentation_example():
    """Full 5-slide deck from Collabora DocumentToolDescriptions.hpp (integration smoke)."""
    try:
        import pytest

        if _test_doc is None:
            pytest.skip("Requires LibreOffice from native runner")
    except ImportError:
        pass

    result = _exec_transform(transform=COLLABORA_FIVE_SLIDE_TRANSFORM_JSON)
    data = json.loads(result)
    assert data.get("status") == "ok", result
    pages = _test_doc.getDrawPages()
    assert pages.getCount() >= 5, "Collabora example inserts 4 slides after the first"
    applied = data.get("applied") or []
    assert count_applied_prefix(applied, "InsertMasterSlide") >= 4
    assert count_applied_prefix(applied, "SetText.") >= 5


def count_applied_prefix(applied: list[str], prefix: str) -> int:
    return sum(1 for entry in applied if prefix in entry)
