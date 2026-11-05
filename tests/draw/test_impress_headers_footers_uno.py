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
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _exec_tool(doc, ctx, name, args):
    res = TestingFactory.execute_tool(doc, ctx, name, args, doc_type="impress")
    return json.dumps(res) if isinstance(res, dict) else res


@native_test
@with_native_doc("impress")
def test_headers_footers(ctx, doc):
    # 1. Get initial headers/footers
    result_str = _exec_tool(doc, ctx, "headers_footers_get", {"page": 0})
    result = json.loads(result_str)

    assert result.get("status") == "ok"
    assert "properties" in result

    # 2. Set headers/footers
    set_result_str = _exec_tool(doc, ctx, "headers_footers_set", {
        "page": 0,
        "footer": "This is a test footer",
        "footer_visible": True,
        "page_number_visible": True,
        "date_time": "2024-01-01",
        "date_time_visible": True,
        "date_time_fixed": True
    })
    set_result = json.loads(set_result_str)

    assert set_result.get("status") == "ok"
    assert set_result.get("updated_properties") > 0

    # 3. Verify changes
    result_str = _exec_tool(doc, ctx, "headers_footers_get", {"page": 0})
    result = json.loads(result_str)

    props = result.get("properties", {})
    assert props.get("FooterText") == "This is a test footer"
    assert props.get("IsFooterVisible") is True
    assert props.get("IsPageNumberVisible") is True
    assert props.get("DateTimeText") == "2024-01-01"
    assert props.get("IsDateTimeVisible") is True
    assert props.get("IsDateTimeFixed") is True

    # 4. Test master page
    set_result_str = _exec_tool(doc, ctx, "headers_footers_set", {
        "page": 0,
        "is_master_page": True,
        "footer": "Master Footer"
    })
    set_result = json.loads(set_result_str)
    assert set_result.get("status") == "ok"

    result_str = _exec_tool(doc, ctx, "headers_footers_get", {"page": 0, "is_master_page": True})
    result = json.loads(result_str)
    props = result.get("properties", {})
    assert props.get("FooterText") == "Master Footer"
