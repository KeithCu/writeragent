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
from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.writer.styles import ApplyStyle, StyleClone, StyleCreate, StyleFormatText, StyleGetInfo, StyleList, StyleUpdate
from plugin.tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("writer")
def test_create_paragraph_style_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    
    style_name = "AgentCustomPara"
    tool = StyleCreate()
    res = tool.execute(tool_ctx, style=style_name, family="ParagraphStyles", parent_style="Standard", property_updates={"CharWeight": 150.0, "CharColor": "#FF0000"})
    
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
    res = tool.execute(tool_ctx, style=style_name, family="ParagraphStyles", conditional_rules=rules)
    
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
    res = tool.execute(tool_ctx, style=style_name, family="ParagraphStyles", parent_style="Heading 1")
    
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
        tool_ctx, style=style_name, family="ParagraphStyles",
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

    res = StyleClone().execute(tool_ctx, style="CloneSrcPara", new_style="CloneDstPara", family="ParagraphStyles")

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


def _portion_fonts(text_obj):
    """Collect (text, font_props) for every non-empty Text portion in *text_obj*, descending into tables."""
    out = []
    para_enum = text_obj.createEnumeration()
    while enum_has_more(para_enum):
        el = para_enum.nextElement()
        if el.supportsService("com.sun.star.text.TextTable"):
            for cname in el.getCellNames():
                out.extend(_portion_fonts(el.getCellByName(cname).getText()))
            continue
        if not el.supportsService("com.sun.star.text.Paragraph"):
            continue
        p_enum = el.createEnumeration()
        while enum_has_more(p_enum):
            portion = p_enum.nextElement()
            if portion.getPropertyValue("TextPortionType") != "Text":
                continue
            s = portion.getString()
            if not s:
                continue
            out.append((s, {
                "CharFontName": str(portion.getPropertyValue("CharFontName")),
                "CharFontNameAsian": str(portion.getPropertyValue("CharFontNameAsian")),
                "CharFontNameComplex": str(portion.getPropertyValue("CharFontNameComplex")),
                "CharHeight": float(portion.getPropertyValue("CharHeight")),
                "CharWeight": float(portion.getPropertyValue("CharWeight")),
                "ParaLeftMargin": int(el.getPropertyValue("ParaLeftMargin")),
            }))
    return out


def _insert_table(doc, text, cur, rows=2, cols=2):
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(rows, cols)
    text.insertTextContent(cur, tbl, False)
    return tbl


@native_test
@with_native_doc("writer")
def test_style_format_text_full_document_reaches_tables_keeps_other_formatting_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("ParaLeftMargin", 1000)
    cur.setPropertyValue("CharFontName", "Liberation Serif")
    cur.setPropertyValue("CharHeight", 14.0)
    cur.setPropertyValue("CharWeight", 150.0)
    text.insertString(cur, "Body text", False)

    tbl = _insert_table(doc, text, cur)
    c_a1 = tbl.getCellByName("A1").getText().createTextCursor()
    c_a1.setPropertyValue("CharFontName", "Liberation Mono")
    tbl.getCellByName("A1").getText().insertString(c_a1, "Cell A1", False)

    nested = _insert_table(doc, tbl.getCellByName("B2").getText(), tbl.getCellByName("B2").getText().createTextCursor(), 1, 1)
    c_n = nested.getCellByName("A1").getText().createTextCursor()
    c_n.setPropertyValue("CharFontName", "Liberation Mono")
    nested.getCellByName("A1").getText().insertString(c_n, "Nested", False)

    before = dict(_portion_fonts(text))
    res = StyleFormatText().execute(tool_ctx, properties={"CharFontName": "Arial"}, target="full_document")
    assert res["status"] == "ok", res
    after = dict(_portion_fonts(text))
    for label in ("Body text", "Cell A1", "Nested"):
        assert after[label]["CharFontName"] == "Arial"
        assert after[label]["CharFontNameAsian"] == before[label]["CharFontNameAsian"]
        assert after[label]["CharFontNameComplex"] == before[label]["CharFontNameComplex"]
    assert after["Body text"]["CharHeight"] == 14.0
    assert after["Body text"]["CharWeight"] == 150.0
    assert after["Body text"]["ParaLeftMargin"] == 1000


@native_test
@with_native_doc("writer")
def test_style_format_text_asian_and_complex_slots_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharFontName", "Liberation Serif")
    text.insertString(cur, "Latin 日本語 عربي", False)

    res_asian = StyleFormatText().execute(
        tool_ctx, properties={"CharFontNameAsian": "Noto Sans CJK JP"}, target="full_document"
    )
    assert res_asian["status"] == "ok", res_asian
    fonts = _portion_fonts(text)[0][1]
    assert fonts["CharFontName"] == "Liberation Serif"
    assert fonts["CharFontNameAsian"] == "Noto Sans CJK JP"

    res_complex = StyleFormatText().execute(
        tool_ctx, properties={"CharFontNameComplex": "Noto Sans Arabic"}, target="full_document"
    )
    assert res_complex["status"] == "ok", res_complex
    fonts = _portion_fonts(text)[0][1]
    assert fonts["CharFontName"] == "Liberation Serif"
    assert fonts["CharFontNameAsian"] == "Noto Sans CJK JP"
    assert fonts["CharFontNameComplex"] == "Noto Sans Arabic"

    res_all = StyleFormatText().execute(
        tool_ctx,
        properties={
            "CharFontName": "Arial",
            "CharFontNameAsian": "Arial",
            "CharFontNameComplex": "Arial",
        },
        target="full_document",
    )
    assert res_all["status"] == "ok", res_all
    fonts = _portion_fonts(text)[0][1]
    assert fonts["CharFontName"] == "Arial"
    assert fonts["CharFontNameAsian"] == "Arial"
    assert fonts["CharFontNameComplex"] == "Arial"


@native_test
@with_native_doc("writer")
def test_style_format_text_reset_shows_style_font_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharFontName", "Liberation Serif")
    cur.setPropertyValue("CharWeight", 150.0)
    text.insertString(cur, "Body", False)

    tbl = _insert_table(doc, text, cur, 1, 1)
    c_a1 = tbl.getCellByName("A1").getText().createTextCursor()
    c_a1.setPropertyValue("CharFontName", "Liberation Serif")
    tbl.getCellByName("A1").getText().insertString(c_a1, "Cell", False)

    su = StyleUpdate().execute(
        tool_ctx, style="Standard", family="ParagraphStyles",
        property_updates={"CharFontName": "DejaVu Sans"},
    )
    assert su["status"] == "ok", su

    res = StyleFormatText().execute(tool_ctx, reset=["CharFontName"], target="full_document")
    assert res["status"] == "ok", res
    assert res["after"]["CharFontName"] == "DejaVu Sans"
    after = dict(_portion_fonts(text))
    assert after["Body"]["CharFontName"] == "DejaVu Sans"
    assert after["Body"]["CharWeight"] == 150.0
    assert after["Cell"]["CharFontName"] == "DejaVu Sans"


@native_test
@with_native_doc("writer")
def test_style_format_text_search_all_matches_leaves_neighbours_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharFontName", "Liberation Serif")
    text.insertString(cur, "alpha beta", False)

    tbl = _insert_table(doc, text, cur, 1, 1)
    c_a1 = tbl.getCellByName("A1").getText().createTextCursor()
    c_a1.setPropertyValue("CharFontName", "Liberation Serif")
    tbl.getCellByName("A1").getText().insertString(c_a1, "alpha beta", False)

    res = StyleFormatText().execute(
        tool_ctx, properties={"CharFontName": "Arial"}, target="search", old_content="beta", all_matches=True,
    )
    assert res["status"] == "ok", res
    assert res["applied_count"] == 2
    portions = _portion_fonts(text)
    beta_fonts = [p[1]["CharFontName"] for p in portions if "beta" in p[0]]
    alpha_fonts = [p[1]["CharFontName"] for p in portions if "alpha" in p[0]]
    assert beta_fonts == ["Arial", "Arial"]
    assert alpha_fonts == ["Liberation Serif", "Liberation Serif"]


@native_test
@with_native_doc("writer")
def test_style_format_text_record_mode_format_redlines_reject_restores_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharFontName", "Liberation Serif")
    text.insertString(cur, "Body", False)

    tbl = _insert_table(doc, text, cur, 1, 1)
    c_a1 = tbl.getCellByName("A1").getText().createTextCursor()
    c_a1.setPropertyValue("CharFontName", "Liberation Mono")
    tbl.getCellByName("A1").getText().insertString(c_a1, "Cell", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        res = StyleFormatText().execute(tool_ctx, properties={"CharFontName": "Arial"}, target="full_document")
        assert res["status"] == "ok", res
        assert res["tracked"] is True

        # Empty paragraphs change font without a redline (zero-length ranges carry no redline).
        redline_types = []
        rl_enum = doc.getRedlines().createEnumeration()
        while enum_has_more(rl_enum):
            redline_types.append(str(rl_enum.nextElement().getPropertyValue("RedlineType")))
        assert redline_types and all(t == "Format" for t in redline_types), redline_types

        helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        frame = doc.getCurrentController().getFrame()
        helper.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())

        restored = dict(_portion_fonts(text))
        assert restored["Body"]["CharFontName"] == "Liberation Serif"
        assert restored["Cell"]["CharFontName"] == "Liberation Mono"

        count_before_reset = doc.getRedlines().getCount()
        res_reset = StyleFormatText().execute(tool_ctx, reset=["CharFontName"], target="full_document")
        assert res_reset["status"] == "ok", res_reset
        assert res_reset["tracked"] is True
        assert "style_unreviewed" not in res_reset
        assert doc.getRedlines().getCount() > count_before_reset
        style_font = doc.getStyleFamilies().getByName("ParagraphStyles").getByName("Standard").getPropertyValue("CharFontName")
        after_reset = dict(_portion_fonts(text))
        assert after_reset["Body"]["CharFontName"] == style_font
        assert after_reset["Cell"]["CharFontName"] == style_font
        helper.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())
        restored_after_reset = dict(_portion_fonts(text))
        assert restored_after_reset["Body"]["CharFontName"] == "Liberation Serif"
        assert restored_after_reset["Cell"]["CharFontName"] == "Liberation Mono"
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_style_format_text_selection_in_table_cell_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharFontName", "Liberation Serif")
    text.insertString(cur, "Body", False)

    tbl = _insert_table(doc, text, cur, 1, 2)
    for cname, label in (("A1", "CellOne"), ("B1", "CellTwo")):
        c_cur = tbl.getCellByName(cname).getText().createTextCursor()
        c_cur.setPropertyValue("CharFontName", "Liberation Serif")
        tbl.getCellByName(cname).getText().insertString(c_cur, label, False)

    a1_cur = tbl.getCellByName("A1").getText().createTextCursor()
    a1_cur.gotoStart(False)
    a1_cur.gotoEnd(True)
    doc.getCurrentController().select(a1_cur)

    res = StyleFormatText().execute(tool_ctx, properties={"CharFontName": "Arial"}, target="selection")
    assert res["status"] == "ok", res
    after = dict(_portion_fonts(text))
    assert after["CellOne"]["CharFontName"] == "Arial"
    assert after["CellTwo"]["CharFontName"] == "Liberation Serif"
    assert after["Body"]["CharFontName"] == "Liberation Serif"


@native_test
@with_native_doc("writer")
def test_style_format_text_paragraph_props_cover_paragraph_and_reset_in_one_undo_step_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "First target word here\rSecond paragraph stays", False)

    paras = []
    para_enum = text.createEnumeration()
    while enum_has_more(para_enum):
        paras.append(para_enum.nextElement())
    assert len(paras) == 2
    orig_adjust_0 = paras[0].getPropertyValue("ParaAdjust")
    orig_margin_0 = int(paras[0].getPropertyValue("ParaLeftMargin"))
    orig_adjust_1 = paras[1].getPropertyValue("ParaAdjust")
    orig_margin_1 = int(paras[1].getPropertyValue("ParaLeftMargin"))

    res = StyleFormatText().execute(
        tool_ctx,
        properties={"ParaAdjust": "center", "ParaLeftMargin": 1000, "CharWeight": 150.0},
        target="search",
        old_content="target",
    )
    assert res["status"] == "ok", res
    assert paras[0].getPropertyValue("ParaAdjust") == 3
    assert int(paras[0].getPropertyValue("ParaLeftMargin")) == 1000
    assert paras[1].getPropertyValue("ParaAdjust") == orig_adjust_1
    assert int(paras[1].getPropertyValue("ParaLeftMargin")) == orig_margin_1

    portions = _portion_fonts(text)
    target_weights = [p[1]["CharWeight"] for p in portions if "target" in p[0]]
    other_weights = [p[1]["CharWeight"] for p in portions if "First" in p[0] or "Second" in p[0]]
    assert target_weights == [150.0]
    assert all(w == 100.0 for w in other_weights)

    undo_mgr = doc.getUndoManager()
    undo_before = len(undo_mgr.getAllUndoActionTitles())
    res_reset = StyleFormatText().execute(
        tool_ctx,
        reset=["ParaAdjust", "ParaLeftMargin", "CharWeight"],
        target="search",
        old_content="target",
    )
    assert res_reset["status"] == "ok", res_reset
    assert len(undo_mgr.getAllUndoActionTitles()) == undo_before + 1
    assert paras[0].getPropertyValue("ParaAdjust") == orig_adjust_0
    assert int(paras[0].getPropertyValue("ParaLeftMargin")) == orig_margin_0
    assert all(p[1]["CharWeight"] == 100.0 for p in _portion_fonts(text))


@native_test
@with_native_doc("writer")
def test_style_format_text_unknown_name_leaves_document_unchanged_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Keep plain", False)

    res = StyleFormatText().execute(
        tool_ctx, properties={"CharWeight": 150.0, "Bogus": 1}, target="full_document"
    )
    assert res["status"] == "error"
    assert res["code"] == "UNKNOWN_PROPERTY"
    assert all(p[1]["CharWeight"] == 100.0 for p in _portion_fonts(text))


@native_test
@with_native_doc("writer")
def test_style_format_text_bad_value_leaves_document_unchanged_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharFontName", "Liberation Serif")
    text.insertString(cur, "Keep font", False)

    undo_mgr = doc.getUndoManager()
    undo_before = len(undo_mgr.getAllUndoActionTitles())
    res = StyleFormatText().execute_safe(
        tool_ctx, properties={"CharFontName": "Arial", "CharWeight": "bold"}, target="full_document"
    )
    assert res["status"] == "error"
    assert "CharFontName" in res["message"] and "CharWeight" in res["message"]
    assert all(p[1]["CharFontName"] == "Liberation Serif" for p in _portion_fonts(text))
    assert len(undo_mgr.getAllUndoActionTitles()) == undo_before


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_mixed_set_and_reset_including_unrecordable_uno(ctx, doc):
    from com.sun.star.style.BreakType import PAGE_BEFORE
    from plugin.framework.config import get_config, save

    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "off"})
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharFontName", "Liberation Mono")
    text.insertString(cur, "Mixed", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        res = StyleFormatText().execute(
            tool_ctx,
            properties={"CharWeight": 150.0, "BreakType": PAGE_BEFORE},
            reset=["CharFontName", "CharStyleName"],
            target="full_document",
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert res["style_unreviewed"] is True
        assert doc.getRedlines().getCount() > 0

        doc.getUndoManager().undo()
        restored = dict(_portion_fonts(text))
        assert restored["Mixed"]["CharFontName"] == "Liberation Mono"
        assert restored["Mixed"]["CharWeight"] == 100.0
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_reset_accept_leaves_style_font_also_after_style_edit_uno(ctx, doc):
    from plugin.framework.config import get_config, save

    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "off"})
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharFontName", "Liberation Mono")
    text.insertString(cur, "Styled", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        res = StyleFormatText().execute(tool_ctx, reset=["CharFontName"], target="full_document")
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        helper.executeDispatch(doc.getCurrentController().getFrame(), ".uno:AcceptAllTrackedChanges", "", 0, ())

        std = doc.getStyleFamilies().getByName("ParagraphStyles").getByName("Standard")
        assert dict(_portion_fonts(text))["Styled"]["CharFontName"] == std.getPropertyValue("CharFontName")

        std.setPropertyValue("CharFontName", "DejaVu Sans")
        assert dict(_portion_fonts(text))["Styled"]["CharFontName"] == "DejaVu Sans"
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_para_reset_is_tracked_and_rejectable_uno(ctx, doc):
    from plugin.framework.config import get_config, save

    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "off"})
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("ParaLeftMargin", 1000)
    text.insertString(cur, "Indented", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        before = doc.getRedlines().getCount()
        res = StyleFormatText().execute(tool_ctx, reset=["ParaLeftMargin"], target="full_document")
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert "style_unreviewed" not in res
        redlines = doc.getRedlines()
        assert redlines.getCount() == before + 1
        assert redlines.getByIndex(before).getPropertyValue("RedlineType") == "ParagraphFormat"

        helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        helper.executeDispatch(doc.getCurrentController().getFrame(), ".uno:RejectAllTrackedChanges", "", 0, ())
        para = text.createEnumeration().nextElement()
        assert abs(para.getPropertyValue("ParaLeftMargin") - 1000) <= 2
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer")
def test_style_format_text_reset_para_adjust_uno(ctx, doc):
    """Resetting ParaAdjust reports the resolved paragraph alignment ('left' or 'start')."""
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Centered text", False)
    para_cur = text.createTextCursor()
    para_cur.gotoStart(False)
    para_cur.gotoEndOfParagraph(True)
    para_cur.setPropertyValue("ParaAdjust", 3)

    res = StyleFormatText().execute(tool_ctx, reset=["ParaAdjust"], target="full_document")
    assert res["status"] == "ok", res
    para = text.createEnumeration().nextElement()
    raw = int(para.getPropertyValue("ParaAdjust"))
    assert res["after"]["ParaAdjust"] == {0: "left", 5: "start"}[raw]


@native_test
@with_native_doc("writer")
def test_apply_no_character_style_clears_visible_style_uno(ctx, doc):
    """Applying 'No Character Style' clears CharStyleName, keeps direct italic, and records a Format redline."""
    from com.sun.star.awt.FontSlant import ITALIC

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    list_res = StyleList().execute(tool_ctx, family="CharacterStyles")
    assert list_res["status"] == "ok", list_res
    assert "No Character Style" in [s["name"] for s in list_res["styles"]]

    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "alpha beta gamma", False)

    beta = text.createTextCursor()
    beta.gotoStart(False)
    beta.goRight(6, False)
    beta.goRight(4, True)
    beta.setPropertyValue("CharStyleName", "Strong Emphasis")
    beta.setPropertyValue("CharPosture", ITALIC)

    res = ApplyStyle().execute(
        tool_ctx,
        style="No Character Style",
        family="CharacterStyles",
        target="search",
        old_content="beta",
    )
    assert res["status"] == "ok", res

    def _beta_portion():
        para = text.createEnumeration().nextElement()
        portions = para.createEnumeration()
        while enum_has_more(portions):
            p = portions.nextElement()
            if p.getString() == "beta":
                return p
        raise AssertionError("beta portion not found")

    p = _beta_portion()
    assert p.getPropertyValue("CharStyleName") == ""
    assert float(p.getPropertyValue("CharWeight")) == 100.0
    assert p.getPropertyValue("CharPosture") == ITALIC

    # With RecordChanges on, clearing records one Format redline and Reject All restores Strong Emphasis.
    beta2 = text.createTextCursor()
    beta2.gotoStart(False)
    beta2.goRight(6, False)
    beta2.goRight(4, True)
    beta2.setPropertyValue("CharStyleName", "Strong Emphasis")

    doc.setPropertyValue("RecordChanges", True)
    try:
        rec_res = ApplyStyle().execute(
            tool_ctx,
            style="No Character Style",
            family="CharacterStyles",
            target="search",
            old_content="beta",
        )
        assert rec_res["status"] == "ok", rec_res
        redlines = doc.getRedlines()
        assert redlines.getCount() == 1
        assert redlines.getByIndex(0).getPropertyValue("RedlineType") == "Format"

        frame = doc.getCurrentController().getFrame()
        dh = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        dh.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())
        assert float(_beta_portion().getPropertyValue("CharWeight")) == 150.0
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_style_update_char_color_int_and_minus_one_uno(ctx, doc):
    from plugin.framework.tool import prepare_tool_call
    from plugin.writer.styles import StyleUpdate

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    tool = StyleUpdate()
    style = doc.getStyleFamilies().getByName("ParagraphStyles").getByName("Standard")

    prep_blue = prepare_tool_call(
        tool,
        tool_ctx,
        "style_update",
        {"style": "Standard", "property_updates": {"CharColor": 255}},
    )
    assert prep_blue.error is None, prep_blue.error
    res_blue = tool.execute(tool_ctx, **prep_blue.kwargs)
    assert res_blue.get("status") == "ok", res_blue
    assert int(style.getPropertyValue("CharColor")) == 255

    prep_auto = prepare_tool_call(
        tool,
        tool_ctx,
        "style_update",
        {"style": "Standard", "property_updates": {"CharColor": -1}},
    )
    assert prep_auto.error is None, prep_auto.error
    res_auto = tool.execute(tool_ctx, **prep_auto.kwargs)
    assert res_auto.get("status") == "ok", res_auto
    assert int(style.getPropertyValue("CharColor")) == -1


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_reset_char_style_name_is_rejectable_uno(ctx, doc):
    from plugin.framework.config import get_config, save

    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "off"})
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharStyleName", "Source Text")
    text.insertString(cur, "code ", False)
    cur.setPropertyValue("CharWeight", 150.0)
    text.insertString(cur, "bold", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        res = StyleFormatText().execute(tool_ctx, reset=["CharStyleName"], target="full_document")
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert "style_unreviewed" not in res
        assert doc.getRedlines().getCount() > 0

        helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        helper.executeDispatch(doc.getCurrentController().getFrame(), ".uno:RejectAllTrackedChanges", "", 0, ())
        para = text.createEnumeration().nextElement()
        penum = para.createEnumeration()
        portions = {}
        while enum_has_more(penum):
            p = penum.nextElement()
            if p.getPropertyValue("TextPortionType") == "Text":
                portions[p.getString()] = (
                    p.getPropertyValue("CharStyleName"),
                    p.getPropertyValue("CharWeight"),
                )
        assert portions["code "] == ("Source Text", 100.0)
        assert portions["bold"] == ("Source Text", 150.0)
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_style_single_target_is_one_undo_step_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("CharWeight", 150.0)
    text.insertString(cur, "one ", False)
    cur.setPropertyValue("CharWeight", 100.0)
    text.insertString(cur, "two ", False)
    cur.setPropertyValue("CharWeight", 150.0)
    text.insertString(cur, "three", False)

    undo = doc.getUndoManager()
    undo.clear()
    res = ApplyStyle().execute(tool_ctx, style="Heading 1", target="full_document", clear_direct="style_props")
    assert res["status"] == "ok", res
    assert len(list(undo.getAllUndoActionTitles())) == 1

    undo.undo()
    para = text.createEnumeration().nextElement()
    assert para.getPropertyValue("ParaStyleName") == "Standard"
    portions = dict(_portion_fonts(text))
    assert portions["one "]["CharWeight"] == 150.0
    assert portions["two "]["CharWeight"] == 100.0
    assert portions["three"]["CharWeight"] == 150.0


@native_test
@with_native_doc("writer")
def test_style_format_text_para_adjust_start_uno(ctx, doc):
    from plugin.tests.testing_utils import libreoffice_version

    text = doc.getText()
    text.setString("Alignment check")
    cur = text.createTextCursor()
    cur.gotoRange(text.getStart(), False)
    cur.gotoRange(text.getEnd(), True)
    cur.setPropertyValue("ParaAdjust", 3)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = StyleFormatText().execute(tool_ctx, properties={"ParaAdjust": "start"}, target="full_document")

    para = text.createEnumeration().nextElement()
    effective = para.getPropertyValue("ParaAdjust")
    assert res["status"] == "ok", res
    if libreoffice_version(ctx) >= (26, 2):
        assert res["after"]["ParaAdjust"] == "start"
        assert effective == 5
    else:
        assert res["after"]["ParaAdjust"] == "left"
        assert effective == 0


