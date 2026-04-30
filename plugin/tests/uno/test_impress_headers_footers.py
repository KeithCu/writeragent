# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import json
from plugin.framework.logging import log
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc = None
_test_ctx = None


@setup
def setup_impress_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    import uno

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )

    _test_doc = desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create Impress document"
    assert hasattr(_test_doc, "getDrawPages"), "Not a valid Impress document"

    log.info("[ImpressTests] test_impress_headers_footers: starting tests")


@teardown
def teardown_impress_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


def _exec_tool(name, args):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool_context import ToolContext
    tctx = ToolContext(_test_doc, _test_ctx, "impress", get_services(), "test")
    res = get_tools().execute(name, tctx, **args)
    return json.dumps(res) if isinstance(res, dict) else res


@native_test
def test_headers_footers():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    # 1. Get initial headers/footers
    result_str = _exec_tool("get_headers_footers", {"page_index": 0})
    result = json.loads(result_str)

    assert result.get("status") == "ok"
    assert "properties" in result

    # 2. Set headers/footers
    set_result_str = _exec_tool("set_headers_footers", {
        "page_index": 0,
        "footer_text": "This is a test footer",
        "is_footer_visible": True,
        "is_page_number_visible": True,
        "date_time_text": "2024-01-01",
        "is_date_time_visible": True,
        "is_date_time_fixed": True
    })
    set_result = json.loads(set_result_str)

    assert set_result.get("status") == "ok"
    assert set_result.get("updated_properties") > 0

    # 3. Verify changes
    result_str = _exec_tool("get_headers_footers", {"page_index": 0})
    result = json.loads(result_str)

    props = result.get("properties", {})
    assert props.get("FooterText") == "This is a test footer"
    assert props.get("IsFooterVisible") is True
    assert props.get("IsPageNumberVisible") is True
    assert props.get("DateTimeText") == "2024-01-01"
    assert props.get("IsDateTimeVisible") is True
    assert props.get("IsDateTimeFixed") is True

    # 4. Test master page
    set_result_str = _exec_tool("set_headers_footers", {
        "page_index": 0,
        "is_master_page": True,
        "footer_text": "Master Footer"
    })
    set_result = json.loads(set_result_str)
    assert set_result.get("status") == "ok"

    result_str = _exec_tool("get_headers_footers", {"page_index": 0, "is_master_page": True})
    result = json.loads(result_str)
    props = result.get("properties", {})
    assert props.get("FooterText") == "Master Footer"
