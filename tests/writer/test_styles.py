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
import sys
from contextlib import contextmanager

import pytest
from unittest.mock import MagicMock, patch
from plugin.writer.page import PageGetStyleProperties
from plugin.writer.styles import (
    StyleList, StyleGetInfo, ApplyStyle, StyleCreate, StyleImport, StyleUpdate,
    _close_style_names,
)
from plugin.tests.testing_utils import TestingFactory, WriterDocStub


def create_mock_style(name, display_name=None, is_in_use=False, is_user_defined=False, is_physical=True, is_hidden=False, category=None):
    style = MagicMock()
    style.isInUse.return_value = is_in_use
    style.isUserDefined.return_value = is_user_defined

    props = {
        "DisplayName": display_name or name,
        "IsPhysical": is_physical,
        "IsHidden": is_hidden,
        "ParentStyle": "Default",
        "Category": category if category is not None else 0,  # Default to TEXT
    }
    style.getPropertyValue.side_effect = lambda p: props.get(p)
    return style


def _style_family(styles_data=None, *, exists=True):
    """MagicMock XNameAccess style family seeded into WriterDocStub.items."""
    family = MagicMock()
    if styles_data is not None:
        family.getElementNames.return_value = list(styles_data.keys())
        family.getByName.side_effect = lambda n: styles_data[n]
        family.hasByName.side_effect = lambda n: n in styles_data
    else:
        family.hasByName.return_value = exists
    return family


def _ctx_with_families(**families):
    """Build ToolContext with WriterDocStub style families (name → family mock)."""
    doc = WriterDocStub(items=families)
    return TestingFactory.create_context(doc=doc, doc_type="writer")


@pytest.fixture
def mock_ctx():
    return TestingFactory.create_context(doc_type="writer")


def test_list_styles_filtering():
    styles_data = {
        "Heading 1": create_mock_style("Heading 1", is_in_use=True, is_physical=True, category=1),
        "Text body": create_mock_style("Text body", is_in_use=False, is_physical=True, category=0),
        "Heading 2": create_mock_style("Heading 2", is_in_use=False, is_physical=False, category=1),
        "Heading 6": create_mock_style("Heading 6", is_in_use=False, is_physical=False, category=1),
        "Heading": create_mock_style("Heading", is_in_use=False, is_physical=True, category=1),
        "List 1": create_mock_style("List 1", is_in_use=False, is_physical=True, category=2),
        "Salutation": create_mock_style("Salutation", is_in_use=False, is_physical=True, category=0),
        "Obscure": create_mock_style("Obscure", is_in_use=False, is_physical=False, category=3),
        "Hidden": create_mock_style("Hidden", is_hidden=True, is_physical=True, category=0),
        "MyStyle": create_mock_style("MyStyle", is_user_defined=True, is_physical=True, category=0),
        "Standard": create_mock_style("Standard", is_in_use=True, is_physical=True, category=0),
        "Default Paragraph Style": create_mock_style("Default Paragraph Style", is_in_use=False, is_physical=True, category=0),
    }
    mock_ctx = _ctx_with_families(
        ParagraphStyles=_style_family(styles_data),
        CharacterStyles=_style_family({}),
        PageStyles=_style_family({}),
    )

    tool = StyleList()

    res = tool.execute(mock_ctx, family="")
    assert "ParagraphStyles" in res["families"]
    assert "CharacterStyles" in res["families"]
    # PageStyles is reported: the page tools take a page-style NAME, and a converted .docx rarely
    # uses "Standard" — without this the only way to learn the real names was to guess and read
    # them off the error message.
    assert "PageStyles" in res["families"]

    res = tool.execute(mock_ctx, family="ParagraphStyles")
    assert res["status"] == "ok"
    names = [s["name"] for s in res["styles"]]
    assert "Heading 1" in names
    assert "Text body" in names
    assert "Heading 2" in names
    assert "MyStyle" in names
    assert "Heading" not in names
    assert "List 1" not in names
    assert "Heading 6" not in names
    assert "Salutation" not in names
    assert "Obscure" not in names
    assert "Hidden" not in names
    assert "Standard" not in names
    assert "Default Paragraph Style" not in names
    assert res["count"] == 4


def test_list_character_styles():
    styles_data = {
        "Default Style": create_mock_style("Default Style", display_name="No Character Style", is_in_use=False, is_physical=False),
        "Source Text": create_mock_style("Source Text", is_in_use=False, is_physical=False),
        "Emphasis": create_mock_style("Emphasis", is_in_use=False, is_physical=False),
        "Rubies": create_mock_style("Rubies", is_in_use=False, is_physical=False),
    }
    mock_ctx = _ctx_with_families(CharacterStyles=_style_family(styles_data))

    tool = StyleList()
    res = tool.execute(mock_ctx, family="CharacterStyles")

    assert res["status"] == "ok"
    names = [s["name"] for s in res["styles"]]
    assert "No Character Style" in names
    assert "Source Text" in names
    assert "Emphasis" not in names
    assert "Rubies" not in names
    assert res["count"] == 2


def test_get_style_info():
    style = create_mock_style("Emphasis", is_in_use=True)
    family = _style_family({"Emphasis": style})
    mock_ctx = _ctx_with_families(CharacterStyles=family)

    tool = StyleGetInfo()
    res = tool.execute(mock_ctx, style="Emphasis", family="CharacterStyles")

    assert res["status"] == "ok"
    assert res["name"] == "Emphasis"
    assert res["is_in_use"] is True


def test_get_style_info_para_adjust_is_word():
    """Inspect must use the same ParaAdjust words as style_update."""
    style = create_mock_style("Text body", is_in_use=True)
    props = {
        "DisplayName": "Text body",
        "IsPhysical": True,
        "IsHidden": False,
        "ParentStyle": "Default",
        "Category": 0,
        "ParaAdjust": 2,
    }
    style.getPropertyValue.side_effect = lambda p: props.get(p)
    style.getParentStyle.return_value = "Default"
    family = _style_family({"Text body": style})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)

    res = StyleGetInfo().execute(mock_ctx, style="Text body", family="ParagraphStyles")

    assert res["status"] == "ok"
    assert res["ParaAdjust"] == "justify"


def _page_style_for_info():
    """Page-style mock with the UNO properties get_page_style_properties reads."""
    style = MagicMock()
    props = {
        "Width": 21000,
        "Height": 29700,
        "IsLandscape": False,
        "LeftMargin": 2000,
        "RightMargin": 2000,
        "TopMargin": 2500,
        "BottomMargin": 2000,
        "GutterMargin": 0,
        "HeaderIsOn": True,
        "FooterIsOn": False,
        "HeaderIsShared": True,
        "FooterIsShared": True,
        "HeaderHeight": 500,
        "FooterHeight": 500,
        "HeaderBodyDistance": 500,
        "FooterBodyDistance": 500,
        "BackColor": 16777215,
        "BackTransparent": True,
        "NumberingType": 4,
        "FootnoteHeight": 0,
        "RegisterParagraphStyle": "",
        "FirstIsShared": False,
        "PageStyleLayout": MagicMock(value=0),
    }
    style.getPropertyValue.side_effect = lambda n: props[n]
    return style


def test_get_style_info_page_styles_returns_page_properties():
    """style_get_info(PageStyles) must answer in-process, not error-bounce to the page tool."""
    style = _page_style_for_info()
    mock_ctx = _ctx_with_families(PageStyles=_style_family({"Standard": style}))

    res = StyleGetInfo().execute(mock_ctx, style="Standard", family="PageStyles")

    assert res["status"] == "ok"
    assert res["family"] == "PageStyles"
    assert res["properties"]["left_margin_mm"] == 20.0
    assert res["properties"]["top_margin_mm"] == 25.0
    assert res["properties"]["header_is_on"] is True
    assert res["properties"]["footer_is_on"] is False
    assert res["properties"]["first_is_shared"] is False
    page = PageGetStyleProperties().execute(mock_ctx, style="Standard")
    assert page["status"] == "ok"
    assert page["properties"] == res["properties"]


def test_apply_style_clear_direct_schema_default_is_style_props():
    """Small models read the schema; default must be the house-font-wins value."""
    schema = ApplyStyle.parameters["properties"]["clear_direct"]
    assert schema["default"] == "style_props"
    assert schema["enum"] == ["none", "style_props", "all"]


def test_resolve_clear_direct_defaults():
    assert ApplyStyle._resolve_clear_direct("ParagraphStyles", None) == "style_props"
    assert ApplyStyle._resolve_clear_direct("ParagraphStyles", "") == "style_props"
    assert ApplyStyle._resolve_clear_direct("ParagraphStyles", "none") == "none"
    assert ApplyStyle._resolve_clear_direct("CharacterStyles", None) == "none"
    assert ApplyStyle._resolve_clear_direct("CharacterStyles", "all") == "all"


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_paragraph(mock_resolve, mock_preserve, mock_ctx):
    cursor = MagicMock()
    mock_resolve.return_value = cursor

    tool = ApplyStyle()
    res = tool.execute(mock_ctx, style="Heading 1", target="selection")

    assert res["status"] == "ok"
    assert res["family"] == "ParagraphStyles"
    mock_preserve.assert_called_once_with(mock_ctx.doc, cursor, "Heading 1", "style_props")
    cursor.setPropertyValue.assert_not_called()


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_clear_direct_is_forwarded(mock_resolve, mock_preserve, mock_ctx):
    """clear_direct reaches the apply helper, which decides what happens to direct formatting."""
    mock_resolve.return_value = MagicMock()
    mock_preserve.return_value = {"direct_formatting": "cleared", "clear_direct": "style_props",
                                  "removed_char_overrides": {"CharFontName": "Times New Roman"},
                                  "cleared_char_properties": ["CharFontName", "CharHeight"],
                                  "cleared_paragraph_properties": ["ParaLeftMargin"]}

    res = ApplyStyle().execute(mock_ctx, style="Heading 1", target="selection", clear_direct="style_props")

    assert res["status"] == "ok"
    assert mock_preserve.call_args[0][3] == "style_props"
    assert res["removed_char_overrides"] == {"CharFontName": "Times New Roman"}
    assert res["cleared_char_properties"] == ["CharFontName", "CharHeight"]
    assert res["cleared_paragraph_properties"] == ["ParaLeftMargin"]
    assert "hint" not in res  # nothing was preserved -> nothing to warn about


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_explicit_none_reports_overrides_without_retry_hint(mock_resolve, mock_preserve, mock_ctx):
    """clear_direct='none' still echoes what was kept. No retry-with-style_props hint: that
    dance was for the old default=none no-op; default already shows the house font."""
    mock_resolve.return_value = MagicMock()
    mock_preserve.return_value = {"direct_formatting": "preserved", "clear_direct": "none",
                                  "preserved_char_overrides": {"CharFontName": "Times New Roman", "CharHeight": 12.0}}

    res = ApplyStyle().execute(mock_ctx, style="Standard", target="selection", clear_direct="none")

    assert res["preserved_char_overrides"]["CharHeight"] == 12.0
    assert "hint" not in res


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_forwards_walk_cap_warning(mock_resolve, mock_preserve, mock_ctx):
    """Portion-capture truncate must reach the tool result, not stay a silent log line."""
    mock_resolve.return_value = MagicMock()
    mock_preserve.return_value = {
        "direct_formatting": "preserved",
        "clear_direct": "none",
        "warning": "Walk stopped after 50000 text portions (cap 50000). Later content was not read, so formatting may be incomplete.",
    }

    res = ApplyStyle().execute(mock_ctx, style="Standard", target="selection")

    assert "incomplete" in res["warning"]


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_default_allowed_on_full_document(mock_resolve, mock_preserve, mock_ctx):
    """Default style_props is allowed on full_document so house font shows without a flag."""
    mock_resolve.return_value = MagicMock()
    mock_preserve.return_value = {"direct_formatting": "cleared", "clear_direct": "style_props"}

    res = ApplyStyle().execute(mock_ctx, style="Standard", target="full_document")

    assert res["status"] == "ok"
    assert mock_preserve.call_args[0][3] == "style_props"


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_clear_direct_all_rejected_on_full_document(mock_resolve, mock_preserve, mock_ctx):
    """Ctrl+M on the whole document would wipe emphasis; force the per-range path."""
    mock_resolve.return_value = MagicMock()

    res = ApplyStyle().execute(mock_ctx, style="Standard", target="full_document", clear_direct="all")

    assert res["status"] == "error"
    assert "full_document" in res["message"]
    mock_preserve.assert_not_called()


@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_clear_direct_rejected_for_character_styles(mock_resolve, mock_ctx):
    """Character styles do not go through the paragraph apply path, so the flag would be a no-op."""
    mock_resolve.return_value = MagicMock()

    res = ApplyStyle().execute(mock_ctx, style="Source Text", family="CharacterStyles",
                               target="selection", clear_direct="all")

    assert res["status"] == "error"
    assert "ParagraphStyles" in res["message"]


@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_rejects_unknown_clear_direct(mock_resolve, mock_ctx):
    mock_resolve.return_value = MagicMock()

    res = ApplyStyle().execute(mock_ctx, style="Standard", target="selection", clear_direct="yes")

    assert res["status"] == "error"
    assert "clear_direct" in res["message"]


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_character(mock_resolve, mock_preserve, mock_ctx):
    cursor = MagicMock()
    mock_resolve.return_value = cursor

    tool = ApplyStyle()
    res = tool.execute(mock_ctx, style="Source Text", family="CharacterStyles", target="search", old_content="code")

    assert res["status"] == "ok"
    assert res["family"] == "CharacterStyles"
    mock_preserve.assert_not_called()
    cursor.setPropertyValue.assert_called_once_with("CharStyleName", "Source Text")


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_default_character_style(mock_resolve, mock_preserve, mock_ctx):
    """Applying 'No Character Style' should set CharStyleName to '' (UNO reset)."""
    cursor = MagicMock()
    mock_resolve.return_value = cursor

    tool = ApplyStyle()
    res = tool.execute(mock_ctx, style="No Character Style", family="CharacterStyles", target="selection")

    assert res["status"] == "ok"
    assert res["style_name"] == "No Character Style"
    assert res["family"] == "CharacterStyles"
    mock_preserve.assert_not_called()
    cursor.setPropertyValue.assert_called_once_with("CharStyleName", "")


def test_update_style_with_parent():
    style = MagicMock()
    style.getParentStyle.return_value = "Standard"
    family = _style_family({"MyStyle": style, "Standard": MagicMock()})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)

    tool = StyleUpdate()
    res = tool.execute(mock_ctx, style="MyStyle", parent_style="Standard", property_updates={"CharWeight": 150})

    assert res["status"] == "ok"
    style.setParentStyle.assert_called_once_with("Standard")
    style.setPropertyValue.assert_called_once_with("CharWeight", 150)
    assert "CharWeight" in res["before"]
    assert res["after"]["ParentStyle"] == "Standard"


def test_style_update_schema_para_adjust_is_words():
    schema = StyleUpdate.parameters["properties"]["property_updates"]["properties"]["ParaAdjust"]
    assert schema["type"] == "string"
    assert schema["enum"] == ["left", "center", "right", "justify"]


def test_update_style_para_adjust_words():
    """ParaAdjust 0/1/2/3 is hostile (1=right); schema is left/center/right/justify."""
    props = {"ParaAdjust": 0, "CharWeight": 100}
    style = MagicMock()
    style.getPropertyValue.side_effect = lambda n: props.get(n)
    style.getParentStyle.return_value = "Standard"

    def _set(name, value):
        props[name] = value

    style.setPropertyValue.side_effect = _set
    family = _style_family({"MyStyle": style})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)

    res = StyleUpdate().execute(mock_ctx, style="MyStyle", property_updates={"ParaAdjust": "center"})

    assert res["status"] == "ok"
    style.setPropertyValue.assert_called_once_with("ParaAdjust", 3)
    assert res["before"]["ParaAdjust"] == "left"
    assert res["after"]["ParaAdjust"] == "center"
    assert res["updated_properties"]["ParaAdjust"] == "center"


def test_update_style_rejects_para_adjust_integer():
    style = MagicMock()
    family = _style_family({"MyStyle": style})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)

    res = StyleUpdate().execute(mock_ctx, style="MyStyle", property_updates={"ParaAdjust": 2})

    assert res["status"] == "error"
    assert "left" in res["message"]
    style.setPropertyValue.assert_not_called()


def test_close_style_names_prefers_exact_then_shortest_prefix():
    """Exact case-insensitive hit wins; otherwise shortest prefix, then substring."""
    names = ["Body Text Indent 2", "Body Text", "Text body"]
    assert _close_style_names("body text", names) == ["Body Text"]
    assert _close_style_names("body", names) == ["Body Text", "Body Text Indent 2"]
    assert _close_style_names("indent", names) == ["Body Text Indent 2"]
    assert _close_style_names("nope", names) == []


def test_update_style_unknown_suggests_close_name():
    style = MagicMock()
    family = _style_family({"Heading 1": style, "Text body": style})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)

    res = StyleUpdate().execute(mock_ctx, style="heading 1", property_updates={"CharWeight": 150})

    assert res["status"] == "error"
    assert "Did you mean 'Heading 1'" in res["message"]


def test_update_style_warns_when_font_not_installed():
    style = MagicMock()
    family = _style_family({"MyStyle": style})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)

    with patch("plugin.writer.styles._installed_font_names", return_value={"liberation serif"}):
        res = StyleUpdate().execute(
            mock_ctx, style="MyStyle", property_updates={"CharFontName": "DefinitelyMissingFont"})

    assert res["status"] == "ok"
    assert "DefinitelyMissingFont" in res["warning"]
    assert "substitute" in res["warning"]


def test_create_style_standard():
    family = _style_family({"Standard": MagicMock()})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)
    new_style = MagicMock()
    mock_ctx.doc._created["com.sun.star.style.ParagraphStyle"] = new_style

    tool = StyleCreate()
    res = tool.execute(mock_ctx, style="NewStyle", parent_style="Standard", property_updates={"CharColor": "#FF0000"})

    assert res["status"] == "ok"
    assert res["service"] == "com.sun.star.style.ParagraphStyle"
    new_style.setParentStyle.assert_called_once_with("Standard")
    new_style.setPropertyValue.assert_any_call("CharColor", 0xFF0000)
    family.insertByName.assert_called_once_with("NewStyle", new_style)


def test_create_style_parent_not_found_suggests():
    family = _style_family({"Standard": MagicMock(), "Heading 1": MagicMock()})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)

    res = StyleCreate().execute(mock_ctx, style="NewStyle", parent_style="heading 1")

    assert res["status"] == "error"
    assert "Did you mean 'Heading 1'" in res["message"]
    family.insertByName.assert_not_called()


def test_create_style_rejects_para_adjust_integer():
    family = _style_family({"Standard": MagicMock()})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)

    res = StyleCreate().execute(
        mock_ctx, style="NewStyle", parent_style="Standard", property_updates={"ParaAdjust": 3})

    assert res["status"] == "error"
    assert "justify" in res["message"] or "center" in res["message"]
    family.insertByName.assert_not_called()


@patch("plugin.writer.styles.NamedValue")
def test_create_style_conditional(mock_nv):
    family = _style_family({"Standard": MagicMock(), "Heading 1": MagicMock()})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)
    new_style = MagicMock()
    mock_ctx.doc._created["com.sun.star.style.ConditionalParagraphStyle"] = new_style

    nv_instance = MagicMock()
    mock_nv.return_value = nv_instance

    tool = StyleCreate()
    rules = [{"context": "Table", "target_style": "Heading 1"}]
    res = tool.execute(mock_ctx, style="CondStyle", conditional_rules=rules)

    assert res["status"] == "ok"
    assert res["service"] == "com.sun.star.style.ConditionalParagraphStyle"

    args, kwargs = new_style.setPropertyValue.call_args_list[-1]
    assert args[0] == "ParaStyleConditions"
    assert isinstance(args[1], tuple)
    assert args[1][0] == nv_instance
    assert nv_instance.Name == "Table"
    assert nv_instance.Value == "Heading 1"


@patch("plugin.writer.styles.uno")
@patch("plugin.writer.styles.PropertyValue")
def test_import_styles(mock_pv, mock_uno, mock_ctx):
    mock_uno.systemPathToFileUrl.return_value = "file:///path/to/doc.ott"

    pv_instance = MagicMock()
    mock_pv.return_value = pv_instance

    tool = StyleImport()
    res = tool.execute(mock_ctx, path="/path/to/doc.ott", overwrite=True)

    assert res["status"] == "ok"
    assert len(mock_ctx.doc._load_styles_calls) == 1
    url, opts = mock_ctx.doc._load_styles_calls[0]
    assert url == "file:///path/to/doc.ott"
    assert isinstance(opts, tuple)


def test_apply_style_uno_skips_windows_leftover_origin_canary() -> None:
    """GHA 34683742049: leftover reuse flipped the origin-detection canary."""
    from pathlib import Path

    src = Path(__file__).with_name(
        "test_apply_style_preserve_inline_uno.py"
    ).read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "apply_style origin canary leftover reuse" in src
    assert "34683742049" in src
    assert 'fmt.setPropertyValue("ParaStyleName", "Standard")' in src
    assert "35466498641" in src


def test_apply_style_selection_failure_at_tool_layer(mock_ctx):
    with patch("plugin.writer.styles.resolve_target_cursor",
               side_effect=ValueError("Could not resolve the current selection")):
        res = ApplyStyle().execute(mock_ctx, style="Heading 1", target="selection")
    assert res["status"] == "error"
    assert "selection" in res["message"].lower()


# --- UNO struct values in update_style's property_updates -------------------
# Regression: ParaLineSpacing={'Mode':0,'Height':100} reached PyUNO as a plain
# dict and failed with "'dict' object has no attribute 'getTypes'", taking the
# whole style update down with it.

class _FakeLineSpacing:
    """Stands in for com.sun.star.style.LineSpacing (fixed field set)."""

    def __init__(self):
        self.Mode = 0
        self.Height = 100


@contextmanager
def _fake_uno_struct_factory():
    """Install a struct factory on the mocked ``uno`` module, then un-install it.

    ``patch()`` would leave ``createUnoStruct`` *materialised* in the MagicMock's
    __dict__, and test_tracking's "Date struct missing" case depends on it being
    absent. Restore the attribute's presence, not just its value.
    """
    uno_mod = sys.modules["uno"]
    had = "createUnoStruct" in uno_mod.__dict__
    previous = uno_mod.__dict__.get("createUnoStruct")
    uno_mod.createUnoStruct = lambda _name: _FakeLineSpacing()
    try:
        yield
    finally:
        if had:
            uno_mod.createUnoStruct = previous
        else:
            uno_mod.__dict__.pop("createUnoStruct", None)


def _normalize_with_fake_struct(updates):
    from plugin.writer import styles

    with _fake_uno_struct_factory():
        return styles._normalize_property_updates(updates)


def test_line_spacing_dict_becomes_a_uno_struct():
    out, err = _normalize_with_fake_struct({"ParaLineSpacing": {"Mode": 0, "Height": 150}})
    assert err is None
    assert not isinstance(out["ParaLineSpacing"], dict)
    assert out["ParaLineSpacing"].Height == 150


def test_line_spacing_mode_accepts_the_word():
    out, err = _normalize_with_fake_struct({"ParaLineSpacing": {"Mode": "fix", "Height": 500}})
    assert err is None
    assert out["ParaLineSpacing"].Mode == 3


def test_unknown_line_spacing_mode_is_refused_with_the_options():
    out, err = _normalize_with_fake_struct({"ParaLineSpacing": {"Mode": "duplo"}})
    assert out == {}
    assert "prop" in err and "duplo" in err


def test_unknown_struct_field_names_the_real_fields():
    out, err = _normalize_with_fake_struct({"ParaLineSpacing": {"Altura": 150}})
    assert out == {}
    assert "Altura" in err and "Height" in err


def test_struct_property_given_a_scalar_is_refused():
    out, err = _normalize_with_fake_struct({"ParaLineSpacing": 150})
    # Not a dict -> left for UNO to reject; the normalizer must not crash.
    assert err is None
    assert out["ParaLineSpacing"] == 150


def test_simple_properties_pass_through_untouched():
    out, err = _normalize_with_fake_struct({"CharWeight": 150, "CharColor": "#FF0000"})
    assert err is None
    assert out == {"CharWeight": 150, "CharColor": "#FF0000"}


# --- struct values must come back as JSON, and sequences must be typed --------
# Regression: update_style applied ParaLineSpacing and then returned the UNO struct in
# before/after, which is not JSON-serializable -- the change landed but the tool
# reported failure. ParaTabStops failed outright: Writer refuses a plain tuple of
# structs; the property needs a typed []com.sun.star.style.TabStop.

class _FakeStruct:
    def __init__(self, **fields):
        for k, v in fields.items():
            setattr(self, k, v)


def test_line_spacing_reads_back_as_a_json_object_with_the_mode_word():
    import json

    from plugin.writer.styles import _schema_prop_value

    out = _schema_prop_value("ParaLineSpacing", _FakeStruct(Mode=0, Height=150))
    assert out == {"Mode": "prop", "Height": 150}
    json.dumps(out)


def test_tab_stops_read_back_as_a_list_of_objects():
    import json

    from plugin.writer.styles import _schema_prop_value

    out = _schema_prop_value("ParaTabStops", (_FakeStruct(Position=2000, Alignment="LEFT", DecimalChar=",", FillChar=" "),))
    assert out == [{"Position": 2000, "Alignment": "LEFT", "DecimalChar": ",", "FillChar": " "}]
    json.dumps(out)


def test_simple_values_read_back_unchanged():
    from plugin.writer.styles import _schema_prop_value

    assert _schema_prop_value("CharWeight", 150) == 150


def test_tab_stops_are_set_through_a_typed_sequence():
    from plugin.writer import styles

    style = MagicMock()
    value = (object(),)
    with patch("uno.invoke", create=True) as invoke, patch("uno.Any", create=True) as any_:
        styles._set_style_property(style, "ParaTabStops", value)
    any_.assert_called_once_with("[]com.sun.star.style.TabStop", value)
    assert invoke.call_args.args[1] == "setPropertyValue"
    style.setPropertyValue.assert_not_called()


def test_plain_properties_use_set_property_value():
    from plugin.writer import styles

    style = MagicMock()
    styles._set_style_property(style, "CharWeight", 150)
    style.setPropertyValue.assert_called_once_with("CharWeight", 150)


# ---- D2: apply_style all_matches / occurrence -------------------------------

def _style_ctx():
    ctx = MagicMock()
    fam = MagicMock()
    fam.hasByName.return_value = True
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam
    return ctx


def test_apply_style_all_matches_applies_to_each():
    from plugin.writer.styles import ApplyStyle

    ranges = [MagicMock(), MagicMock(), MagicMock()]
    with patch("plugin.writer.search.find_all_ranges", return_value=ranges), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.format.content_has_markup", return_value=False), \
         patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as ap, \
         patch("plugin.writer.edit_review.review_recording_enabled", return_value=False):
        res = ApplyStyle().execute(_style_ctx(), style="Heading 1", target="search",
                                   old_content="Title", all_matches=True)
    assert res["status"] == "ok" and res["applied_count"] == 3
    assert ap.call_count == 3


def test_apply_style_occurrence_out_of_range():
    from plugin.writer.styles import ApplyStyle

    with patch("plugin.writer.search.find_all_ranges", return_value=[MagicMock()]), \
         patch("plugin.writer.search.normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyStyle().execute(_style_ctx(), style="Heading 1", target="search",
                                   old_content="Title", occurrence=5)
    assert res["status"] == "error" and "out of range" in res["message"]

# ---- 8) recoverable error messages ----------------------------------------------

def test_apply_style_unknown_style_lists_names_and_suggests():
    from plugin.writer.styles import ApplyStyle

    fam = MagicMock()
    fam.hasByName.return_value = False
    fam.getElementNames.return_value = ["Heading 1", "Heading 2", "Text body", "Quotations"]
    ctx = MagicMock()
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam
    res = ApplyStyle().execute(ctx, style="heading 1", family="ParagraphStyles")
    assert res["status"] == "error"
    assert "Did you mean 'Heading 1'" in res["message"]
    assert "Text body" in res["message"]
