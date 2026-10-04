from unittest.mock import MagicMock
from plugin.ppt_master.adapter.uno_pptx_import import _import_slides_from_source


def test_import_slides_from_source_aborts_on_stop():
    ctx = MagicMock()
    target_doc = MagicMock()
    source_doc = MagicMock()

    pages = MagicMock()
    pages.getCount.return_value = 2
    source_doc.getDrawPages.return_value = pages

    stop_checker = MagicMock(return_value=True)

    result = _import_slides_from_source(ctx, target_doc, source_doc, stop_checker=stop_checker)
    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"
