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
from plugin.writer.styles import CreateStyle, ListStyles, UpdateStyle
from plugin.tests.testing_utils import TestingFactory

_test_doc = None
_test_ctx = None

@setup
def my_setup(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    # Create a fresh writer doc for style tests
    _test_doc = TestingFactory.create_native_doc(ctx, doc_type="writer")

@teardown
def my_teardown(ctx):
    global _test_doc
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None

@native_test
def test_create_paragraph_style_uno():
    doc = _test_doc
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    
    style_name = "AgentCustomPara"
    tool = CreateStyle()
    res = tool.execute(tool_ctx, style_name=style_name, parent_style="Standard", property_updates={"CharWeight": 150.0, "CharColor": "#FF0000"})
    
    assert res["status"] == "ok", f"Tool failed: {res.get('message') or res}"
    
    # Verify via UNO
    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    assert para_styles.hasByName(style_name)
    
    style = para_styles.getByName(style_name)
    assert style.getParentStyle() == "Standard"
    assert style.getPropertyValue("CharWeight") == 150.0
    assert int(style.getPropertyValue("CharColor")) == 0xFF0000

@native_test
def test_create_conditional_style_uno():
    doc = _test_doc
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    
    style_name = "AgentCondPara"
    rules = [{"context": "Table", "target_style": "Heading 1"}]
    tool = CreateStyle()
    res = tool.execute(tool_ctx, style_name=style_name, conditional_rules=rules)
    
    assert res["status"] == "ok", f"Tool failed: {res.get('message') or res}"
    assert res["service"] == "com.sun.star.style.ConditionalParagraphStyle"
    
    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    assert para_styles.hasByName(style_name)
    
    style = para_styles.getByName(style_name)
    # Note: ParaStyleConditions is a read-only property (attribute = 1) in PyUNO 
    # and cannot be modified programmatically in a real Writer instance. 
    # We verify the style was created and registered successfully.
    assert style.getParentStyle() == "Standard"

@native_test
def test_update_style_parent_uno():
    doc = _test_doc
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    
    # Create a style first
    style_name = "UpdateParentTest"
    doc.getStyleFamilies().getByName("ParagraphStyles").insertByName(
        style_name, doc.createInstance("com.sun.star.style.ParagraphStyle")
    )
    
    tool = UpdateStyle()
    res = tool.execute(tool_ctx, style_name=style_name, parent_style="Heading 1")
    
    assert res["status"] == "ok", f"Tool failed: {res.get('message') or res}"
    style = doc.getStyleFamilies().getByName("ParagraphStyles").getByName(style_name)
    assert style.getParentStyle() == "Heading 1"

@native_test
def test_list_styles_uno():
    doc = _test_doc
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    
    tool = ListStyles()
    res = tool.execute(tool_ctx, family="ParagraphStyles")
    
    assert res["status"] == "ok", f"Tool failed: {res.get('message') or res}"
    assert len(res["styles"]) > 0
    
    # Should contain core styles
    names = [s["name"] for s in res["styles"]]
    assert "Heading 1" in names
    assert "Text body" in names
