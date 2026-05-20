# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
import uno
from plugin.testing_runner import native_test, setup, teardown
from plugin.writer.styles import GetStyleInfo
from plugin.tests.testing_utils import TestingFactory
import json

_test_doc = None
_test_ctx = None

@setup
def my_setup(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    _test_doc = TestingFactory.create_native_doc(ctx, doc_type="writer")

@teardown
def my_teardown(ctx):
    global _test_doc
    if _test_doc:
        _test_doc.close(True)

@native_test
def test_inspect_heading1_properties():
    doc = _test_doc
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    
    tool = GetStyleInfo()
    res = tool.execute(tool_ctx, style_name="Heading 1", family="ParagraphStyles")
    
    # Print all keys to find the parent style property
    print("STYLE PROPERTIES:", list(res.get("properties", {}).keys()))
    if "ParentStyle" in res.get("properties", {}):
        print("Found ParentStyle:", res["properties"]["ParentStyle"])
    elif "ParentStyleName" in res.get("properties", {}):
        print("Found ParentStyleName:", res["properties"]["ParentStyleName"])
    
    # Also check the object directly
    style = doc.getStyleFamilies().getByName("ParagraphStyles").getByName("Heading 1")
    print("HAS ParentStyle:", hasattr(style, "ParentStyle"))
    try:
        print("ParentStyle value:", style.ParentStyle)
    except Exception as e:
        print("ParentStyle error:", e)
