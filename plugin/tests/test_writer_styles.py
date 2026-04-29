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
from plugin.modules.writer.styles import ListStyles, GetStyleInfo, ApplyStyle

@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    return ctx

def create_mock_style(name, display_name=None, is_in_use=False, is_user_defined=False, is_physical=True, is_hidden=False, category=None):
    style = MagicMock()
    style.isInUse.return_value = is_in_use
    style.isUserDefined.return_value = is_user_defined
    
    props = {
        "DisplayName": display_name or name,
        "IsPhysical": is_physical,
        "IsHidden": is_hidden,
        "ParentStyle": "Default",
        "Category": category if category is not None else 0 # Default to TEXT
    }
    style.getPropertyValue.side_effect = lambda p: props.get(p)
    return style

def test_list_styles_filtering(mock_ctx):
    families = mock_ctx.doc.getStyleFamilies()
    style_family = MagicMock()
    families.getByName.return_value = style_family
    families.hasByName.return_value = True

    # Setup styles
    # 1. Used built-in (Heading 1)
    # 2. Unused physical built-in (Text Body)
    # 3. Unused non-physical CHAPTER style (Heading 2) - SHOULD BE INCLUDED NOW
    # 4. Unused non-physical INDEX style (Obscure) - SHOULD STILL BE EXCLUDED
    # 5. Hidden style
    # 6. User defined style
    
    styles_data = {
        "Heading 1": create_mock_style("Heading 1", is_in_use=True, is_physical=True, category=1),
        "Text body": create_mock_style("Text body", is_in_use=False, is_physical=True, category=0),
        "Heading 2": create_mock_style("Heading 2", is_in_use=False, is_physical=False, category=1),
        "Heading 6": create_mock_style("Heading 6", is_in_use=False, is_physical=False, category=1), # Should be excluded
        "Heading": create_mock_style("Heading", is_in_use=False, is_physical=True, category=1), # Should be excluded
        "List 1": create_mock_style("List 1", is_in_use=False, is_physical=True, category=2), # Should be excluded
        "Salutation": create_mock_style("Salutation", is_in_use=False, is_physical=True, category=0), # Should be excluded (not core)
        "Obscure": create_mock_style("Obscure", is_in_use=False, is_physical=False, category=3),
        "Hidden": create_mock_style("Hidden", is_hidden=True, is_physical=True, category=0),
        "MyStyle": create_mock_style("MyStyle", is_user_defined=True, is_physical=True, category=0),
        "Standard": create_mock_style("Standard", is_in_use=True, is_physical=True, category=0),
        "Default Paragraph Style": create_mock_style("Default Paragraph Style", is_in_use=False, is_physical=True, category=0),
    }
    
    style_family.getElementNames.return_value = list(styles_data.keys())
    style_family.getByName.side_effect = lambda n: styles_data[n]

    tool = ListStyles()
    
    # Test listing families (should only show Para/Char)
    families.getElementNames.return_value = ["ParagraphStyles", "CharacterStyles", "PageStyles"]
    res = tool.execute(mock_ctx, family="")
    assert "ParagraphStyles" in res["families"]
    assert "CharacterStyles" in res["families"]
    assert "PageStyles" not in res["families"]

    # Test automatic filtering (now the only mode)
    # Should include: Heading 1, Text body, Heading 2, MyStyle
    # Should exclude: Heading (abstract), List 1 (Category 2), Heading 6 (Deep), Salutation (Not Core), Obscure, Hidden
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
    assert res["total_count"] == 12

def test_list_character_styles(mock_ctx):
    families = mock_ctx.doc.getStyleFamilies()
    style_family = MagicMock()
    families.getByName.return_value = style_family
    families.hasByName.return_value = True

    # Setup styles
    # 1. Non-physical core style (Default Style / "No Character Style")
    # 2. Non-physical core style (Source Text)
    # 3. Non-physical pruned style (Emphasis)
    # 4. Non-physical obscure style (Rubies)
    styles_data = {
        "Default Style": create_mock_style("Default Style", display_name="No Character Style", is_in_use=False, is_physical=False),
        "Source Text": create_mock_style("Source Text", is_in_use=False, is_physical=False),
        "Emphasis": create_mock_style("Emphasis", is_in_use=False, is_physical=False),
        "Rubies": create_mock_style("Rubies", is_in_use=False, is_physical=False),
    }
    
    style_family.getElementNames.return_value = list(styles_data.keys())
    style_family.getByName.side_effect = lambda n: styles_data[n]

    tool = ListStyles()
    res = tool.execute(mock_ctx, family="CharacterStyles")
    
    assert res["status"] == "ok"
    names = [s["name"] for s in res["styles"]]
    assert "No Character Style" in names
    assert "Source Text" in names
    assert "Emphasis" not in names
    assert "Rubies" not in names
    assert res["count"] == 2

def test_get_style_info(mock_ctx):
    families = mock_ctx.doc.getStyleFamilies()
    style_family = MagicMock()
    families.getByName.return_value = style_family
    families.hasByName.return_value = True
    
    style = create_mock_style("Emphasis", is_in_use=True)
    style_family.hasByName.return_value = True
    style_family.getByName.return_value = style
    
    tool = GetStyleInfo()
    res = tool.execute(mock_ctx, style_name="Emphasis", family="CharacterStyles")
    
    assert res["status"] == "ok"
    assert res["name"] == "Emphasis"
    assert res["is_in_use"] is True

@patch("plugin.modules.writer.styles.resolve_target_cursor")
def test_apply_style_paragraph(mock_resolve, mock_ctx):
    cursor = MagicMock()
    mock_resolve.return_value = cursor
    
    tool = ApplyStyle()
    res = tool.execute(mock_ctx, style_name="Heading 1", target="selection")
    
    assert res["status"] == "ok"
    assert res["family"] == "ParagraphStyles"
    cursor.setPropertyValue.assert_called_once_with("ParaStyleName", "Heading 1")

@patch("plugin.modules.writer.styles.resolve_target_cursor")
def test_apply_style_character(mock_resolve, mock_ctx):
    cursor = MagicMock()
    mock_resolve.return_value = cursor
    
    tool = ApplyStyle()
    res = tool.execute(mock_ctx, style_name="Source Text", family="CharacterStyles", target="search", old_content="code")
    
    assert res["status"] == "ok"
    assert res["family"] == "CharacterStyles"
    cursor.setPropertyValue.assert_called_once_with("CharStyleName", "Source Text")

@patch("plugin.modules.writer.styles.resolve_target_cursor")
def test_apply_default_character_style(mock_resolve, mock_ctx):
    """Applying 'No Character Style' should set CharStyleName to '' (UNO reset)."""
    cursor = MagicMock()
    mock_resolve.return_value = cursor
    
    tool = ApplyStyle()
    res = tool.execute(mock_ctx, style_name="No Character Style", family="CharacterStyles", target="selection")
    
    assert res["status"] == "ok"
    assert res["style_name"] == "No Character Style"
    assert res["family"] == "CharacterStyles"
    cursor.setPropertyValue.assert_called_once_with("CharStyleName", "")
