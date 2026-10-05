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
from plugin.testing_runner import native_test
from plugin.writer.styles import StyleCreate, StyleList, StyleUpdate
from plugin.tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("writer")
def test_create_paragraph_style_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    
    style_name = "AgentCustomPara"
    tool = StyleCreate()
    res = tool.execute(tool_ctx, style=style_name, parent_style="Standard", property_updates={"CharWeight": 150.0, "CharColor": "#FF0000"})
    
    assert res["status"] == "ok", f"Tool failed: {res.get('message') or res}"
    
    # Verify via UNO
    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    assert para_styles.hasByName(style_name)
    
    style = para_styles.getByName(style_name)
    assert style.getParentStyle() == "Standard"
    assert style.getPropertyValue("CharWeight") == 150.0
    assert int(style.getPropertyValue("CharColor")) == 0xFF0000


@native_test
@with_native_doc("writer")
def test_create_conditional_style_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    
    style_name = "AgentCondPara"
    rules = [{"context": "Table", "target_style": "Heading 1"}]
    tool = StyleCreate()
    res = tool.execute(tool_ctx, style=style_name, conditional_rules=rules)
    
    # In PyUNO, ParaStyleConditions is a read-only property (attribute = 1)
    # and cannot be modified programmatically in a real Writer instance.
    # Therefore, the execution should fail with an error because we explicitly
    # enforce that conditional styles shouldn't be silently dropped.
    assert res["status"] == "error", f"Tool should have failed: {res}"
    assert "Failed to apply conditional rules" in res["message"]


@native_test
@with_native_doc("writer")
def test_update_style_parent_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    
    # Create a style first
    style_name = "UpdateParentTest"
    doc.getStyleFamilies().getByName("ParagraphStyles").insertByName(
        style_name, doc.createInstance("com.sun.star.style.ParagraphStyle")
    )
    
    tool = StyleUpdate()
    res = tool.execute(tool_ctx, style=style_name, parent_style="Heading 1")
    
    assert res["status"] == "ok", f"Tool failed: {res.get('message') or res}"
    style = doc.getStyleFamilies().getByName("ParagraphStyles").getByName(style_name)
    assert style.getParentStyle() == "Heading 1"


@native_test
@with_native_doc("writer")
def test_update_style_para_adjust_and_missing_font_uno(ctx, doc):
    """ParaAdjust words write the UNO integer; a missing font is stored with a warning."""
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    style_name = "AgentAdjustFont"
    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    para_styles.insertByName(style_name, doc.createInstance("com.sun.star.style.ParagraphStyle"))

    res = StyleUpdate().execute(
        tool_ctx, style=style_name,
        property_updates={"ParaAdjust": "center", "CharFontName": "DefinitelyMissingFontXYZ"},
    )
    assert res["status"] == "ok", f"Tool failed: {res.get('message') or res}"
    style = para_styles.getByName(style_name)
    assert int(style.getPropertyValue("ParaAdjust")) == 3
    assert res["after"]["ParaAdjust"] == "center"
    assert style.getPropertyValue("CharFontName") == "DefinitelyMissingFontXYZ"
    assert "DefinitelyMissingFontXYZ" in (res.get("warning") or "")


@native_test
@with_native_doc("writer")
def test_list_styles_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    
    tool = StyleList()
    res = tool.execute(tool_ctx, family="ParagraphStyles")
    
    assert res["status"] == "ok", f"Tool failed: {res.get('message') or res}"
    assert len(res["styles"]) > 0
    
    # Should contain core styles
    names = [s["name"] for s in res["styles"]]
    assert "Heading 1" in names
    assert "Text body" in names
