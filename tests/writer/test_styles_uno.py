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
from plugin.writer.styles import StyleClone, StyleCreate, StyleGetInfo, StyleList, StyleUpdate
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
    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    created = doc.createInstance("com.sun.star.style.ParagraphStyle")
    para_styles.insertByName(style_name, created)
    
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
    created = doc.createInstance("com.sun.star.style.ParagraphStyle")
    para_styles.insertByName(style_name, created)

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


def _drop_style(family, name):
    """Pooled native docs keep user styles across tests. Drop leftovers first."""
    if family.hasByName(name):
        family.removeByName(name)


@native_test
@with_native_doc("writer")
def test_clone_paragraph_style_uno(ctx, doc):
    """Sibling paragraph clone keeps parent, FollowStyle, and a direct override (probe C9)."""
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    para = doc.getStyleFamilies().getByName("ParagraphStyles")
    _drop_style(para, "CloneSrcPara")
    _drop_style(para, "CloneDstPara")
    src = doc.createInstance("com.sun.star.style.ParagraphStyle")
    src.setParentStyle("Heading 1")
    para.insertByName("CloneSrcPara", src)
    src.setPropertyValue("FollowStyle", "Text body")
    src.setPropertyValue("CharWeight", 150.0)
    src.setPropertyValue("ParaTopMargin", 400)

    res = StyleClone().execute(tool_ctx, style="CloneSrcPara", new_style="CloneDstPara")

    assert res["status"] == "ok", res
    assert res["properties_copied"] >= 1
    dst = para.getByName("CloneDstPara")
    assert dst.getParentStyle() == "Heading 1"
    assert dst.getPropertyValue("FollowStyle") == "Text body"
    assert float(dst.getPropertyValue("CharWeight")) == 150.0
    assert int(dst.getPropertyValue("ParaTopMargin")) == 400


@native_test
@with_native_doc("writer")
def test_clone_character_style_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    fam = doc.getStyleFamilies().getByName("CharacterStyles")
    _drop_style(fam, "CloneSrcChar")
    _drop_style(fam, "CloneDstChar")
    src = doc.createInstance("com.sun.star.style.CharacterStyle")
    fam.insertByName("CloneSrcChar", src)
    src.setPropertyValue("CharWeight", 150.0)
    parent = src.getParentStyle()

    res = StyleClone().execute(
        tool_ctx, style="CloneSrcChar", new_style="CloneDstChar", family="CharacterStyles",
    )

    assert res["status"] == "ok", res
    dst = fam.getByName("CloneDstChar")
    assert dst.getParentStyle() == parent
    assert float(dst.getPropertyValue("CharWeight")) == 150.0


@native_test
@with_native_doc("writer")
def test_clone_page_style_uno(ctx, doc):
    """Page clone is an independent style. FollowStyle stays the clone's own name (probe C3)."""
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    pages = doc.getStyleFamilies().getByName("PageStyles")
    _drop_style(pages, "ClonePageSrc")
    _drop_style(pages, "ClonePageDst")
    src = doc.createInstance("com.sun.star.style.PageStyle")
    pages.insertByName("ClonePageSrc", src)
    src.setPropertyValue("LeftMargin", 1234)

    res = StyleClone().execute(
        tool_ctx, style="ClonePageSrc", new_style="ClonePageDst", family="PageStyles",
    )

    assert res["status"] == "ok", res
    dst = pages.getByName("ClonePageDst")
    assert dst.getPropertyValue("FollowStyle") == "ClonePageDst"
    # Twip rounding: 1234 can read back as 1235 (probe C3). Compare to the source read-back.
    assert abs(int(dst.getPropertyValue("LeftMargin")) - int(src.getPropertyValue("LeftMargin"))) <= 2


@native_test
@with_native_doc("writer")
def test_para_back_color_hex_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    style_name = "AgentBackColorTest"
    fam = doc.getStyleFamilies().getByName("ParagraphStyles")
    _drop_style(fam, style_name)

    res = StyleCreate().execute(
        tool_ctx, style=style_name, family="ParagraphStyles",
        property_updates={"ParaBackColor": "#00FF00"},
    )
    assert res["status"] == "ok", f"Tool failed: {res}"
    style = fam.getByName(style_name)
    assert int(style.getPropertyValue("ParaBackColor")) == 0x00FF00

    res2 = StyleUpdate().execute(
        tool_ctx, style=style_name, family="ParagraphStyles",
        property_updates={"ParaBackColor": -1},
    )
    assert res2["status"] == "ok", f"Tool failed: {res2}"
    assert int(style.getPropertyValue("ParaBackColor")) == -1


@native_test
@with_native_doc("writer")
def test_char_posture_roundtrip_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    style_name = "AgentPostureTest"
    fam = doc.getStyleFamilies().getByName("ParagraphStyles")
    _drop_style(fam, style_name)

    res = StyleCreate().execute(
        tool_ctx, style=style_name, family="ParagraphStyles",
        property_updates={"CharPosture": 2},
    )
    assert res["status"] == "ok", f"Tool failed: {res}"

    info = StyleGetInfo().execute(tool_ctx, style=style_name, family="ParagraphStyles")
    assert info["status"] == "ok"
    assert info["CharPosture"] == 2

    style = fam.getByName(style_name)
    uno_val = style.getPropertyValue("CharPosture")
    if hasattr(uno_val, "value"):
        assert uno_val.value == "ITALIC"
    else:
        assert int(uno_val) == 2

