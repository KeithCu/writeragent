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
import pytest
from unittest.mock import MagicMock, patch
from plugin.writer.styles import StyleList, StyleGetInfo, ApplyStyle, StyleCreate, StyleImport, StyleUpdate
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


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_paragraph(mock_resolve, mock_preserve, mock_ctx):
    cursor = MagicMock()
    mock_resolve.return_value = cursor

    tool = ApplyStyle()
    res = tool.execute(mock_ctx, style="Heading 1", target="selection")

    assert res["status"] == "ok"
    assert res["family"] == "ParagraphStyles"
    mock_preserve.assert_called_once_with(mock_ctx.doc, cursor, "Heading 1", "none")
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
def test_apply_style_reports_preserved_overrides_with_hint(mock_resolve, mock_preserve, mock_ctx):
    """The default path echoes the direct formatting that overrode the style — otherwise a style
    apply that changed nothing on screen is indistinguishable from one that worked."""
    mock_resolve.return_value = MagicMock()
    mock_preserve.return_value = {"direct_formatting": "preserved", "clear_direct": "none",
                                  "preserved_char_overrides": {"CharFontName": "Times New Roman", "CharHeight": 12.0}}

    res = ApplyStyle().execute(mock_ctx, style="Standard", target="selection")

    assert res["preserved_char_overrides"]["CharHeight"] == 12.0
    assert "clear_direct='style_props'" in res["hint"]


@patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char")
@patch("plugin.writer.styles.resolve_target_cursor")
def test_apply_style_clear_direct_rejected_on_full_document(mock_resolve, mock_preserve, mock_ctx):
    """Clearing the whole document erases the very formatting used to tell paragraph kinds apart."""
    mock_resolve.return_value = MagicMock()

    res = ApplyStyle().execute(mock_ctx, style="Standard", target="full_document", clear_direct="style_props")

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
    family = _style_family({"MyStyle": style})
    mock_ctx = _ctx_with_families(ParagraphStyles=family)

    tool = StyleUpdate()
    res = tool.execute(mock_ctx, style="MyStyle", parent_style="Standard", property_updates={"CharWeight": 150})

    assert res["status"] == "ok"
    style.setParentStyle.assert_called_once_with("Standard")
    style.setPropertyValue.assert_called_once_with("CharWeight", 150)


def test_create_style_standard():
    family = _style_family(exists=False)
    family.hasByName.return_value = False
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


@patch("plugin.writer.styles.NamedValue")
def test_create_style_conditional(mock_nv):
    family = _style_family(exists=False)
    family.hasByName.return_value = False
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


def test_apply_style_selection_failure_at_tool_layer(mock_ctx):
    with patch("plugin.writer.styles.resolve_target_cursor",
               side_effect=ValueError("Could not resolve the current selection")):
        res = ApplyStyle().execute(mock_ctx, style="Heading 1", target="selection")
    assert res["status"] == "error"
    assert "selection" in res["message"].lower()
