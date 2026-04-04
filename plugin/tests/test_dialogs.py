import pytest
import sys
import types
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

# Mocks specific to UI and dialogs missing from setup_uno_mocks
class MockXEventListener:
    pass

class MockXTransferable:
    pass

class MockXControlContainer:
    pass

class MockXItemListener:
    pass

setattr(sys.modules["com.sun.star.lang"], "XEventListener", MockXEventListener)
setattr(sys.modules["com.sun.star.awt"], "XControlContainer", MockXControlContainer)
setattr(sys.modules["com.sun.star.awt"], "XItemListener", MockXItemListener)
setattr(sys.modules["com.sun.star.datatransfer"], "XTransferable", MockXTransferable)


# Important: We need to mock `_` inside `plugin.framework.dialogs` directly,
# since it uses `from plugin.framework.i18n import _` inside some functions.
# A simpler way is to mock `plugin.framework.i18n._` and `plugin.framework.dialogs._`.

from plugin.framework.dialogs import (
    _uno_impl_to_control_type,
    _xcc,
    add_dialog_button,
    add_dialog_label,
    add_dialog_edit,
    add_dialog_hyperlink,
    translate_dialog
)


@pytest.fixture(autouse=True)
def _restore_com_sun_star_for_dialog_tests():
    """Re-install UNO stubs before each test.

    Other modules (e.g. ``test_tool_loop``, ``test_image_tools_cursor``) assign
    ``sys.modules['com.sun.star.awt']`` at import time without
    ``XControlContainer``, which breaks ``from com.sun.star.awt import
    XControlContainer`` in ``dialogs._xcc`` depending on collection order.
    """
    setup_uno_mocks()
    setattr(sys.modules["com.sun.star.lang"], "XEventListener", MockXEventListener)
    setattr(sys.modules["com.sun.star.awt"], "XControlContainer", MockXControlContainer)
    setattr(sys.modules["com.sun.star.awt"], "XItemListener", MockXItemListener)
    setattr(sys.modules["com.sun.star.datatransfer"], "XTransferable", MockXTransferable)
    yield


def test_uno_impl_to_control_type():
    assert _uno_impl_to_control_type("stardiv.Toolkit.UnoButtonControl") == "Button"
    assert _uno_impl_to_control_type("stardiv.Toolkit.UnoFixedTextControl") == "FixedText"
    assert _uno_impl_to_control_type("UnoControlButton") == "Button"
    assert _uno_impl_to_control_type("UnoControlFixedText") == "FixedText"
    assert _uno_impl_to_control_type("stardiv.Toolkit.UnoComboBoxControl") == "ComboBox"
    # stardiv.Toolkit.UnoControlDialog doesn't match len(seg) > 10 in _uno_impl_to_control_type,
    # UnoControlDialog -> Dialog
    assert _uno_impl_to_control_type("stardiv.Toolkit.UnoControlDialog") == "Dialog"

def test_xcc():
    mock_ctrl = MagicMock()
    mock_xcc = MagicMock()
    mock_ctrl.queryInterface.return_value = mock_xcc

    # Should return what queryInterface returns
    assert _xcc(mock_ctrl) == mock_xcc

    # Should handle queryInterface raising exception
    mock_ctrl.queryInterface.side_effect = Exception("No interface")
    assert _xcc(mock_ctrl) is None

    # Should handle None
    assert _xcc(None) is None

@patch("plugin.framework.dialogs._")
@patch("plugin.framework.i18n._")
def test_add_dialog_button(mock_i18n_translate, mock_translate):
    mock_translate.side_effect = lambda x: f"T_{x}"
    mock_i18n_translate.side_effect = lambda x: f"T_{x}"
    mock_dlg_model = MagicMock()
    mock_btn = MagicMock()
    mock_dlg_model.createInstance.return_value = mock_btn

    btn = add_dialog_button(
        mock_dlg_model, "TestBtn", "Click Me", 10, 20, 100, 30, push_button_type=1, enabled=False
    )

    mock_dlg_model.createInstance.assert_called_once_with("com.sun.star.awt.UnoControlButtonModel")
    assert btn.Name == "TestBtn"
    assert btn.PositionX == 10
    assert btn.PositionY == 20
    assert btn.Width == 100
    assert btn.Height == 30
    assert btn.Label == "T_Click Me"
    assert btn.Enabled is False
    assert btn.PushButtonType == 1
    mock_dlg_model.insertByName.assert_called_once_with("TestBtn", mock_btn)

@patch("plugin.framework.dialogs._")
@patch("plugin.framework.i18n._")
def test_add_dialog_label(mock_i18n_translate, mock_translate):
    mock_translate.side_effect = lambda x: f"T_{x}"
    mock_i18n_translate.side_effect = lambda x: f"T_{x}"
    mock_dlg_model = MagicMock()
    mock_lbl = MagicMock()
    mock_dlg_model.createInstance.return_value = mock_lbl

    lbl = add_dialog_label(
        mock_dlg_model, "TestLbl", "Hello Label", 5, 15, 50, 20, multiline=False
    )

    mock_dlg_model.createInstance.assert_called_once_with("com.sun.star.awt.UnoControlFixedTextModel")
    assert lbl.Name == "TestLbl"
    assert lbl.PositionX == 5
    assert lbl.PositionY == 15
    assert lbl.Width == 50
    assert lbl.Height == 20
    assert lbl.MultiLine is False
    assert lbl.Label == "T_Hello Label"
    mock_dlg_model.insertByName.assert_called_once_with("TestLbl", mock_lbl)

def test_add_dialog_edit():
    mock_dlg_model = MagicMock()
    mock_edit = MagicMock()
    mock_dlg_model.createInstance.return_value = mock_edit

    edit = add_dialog_edit(
        mock_dlg_model, "TestEdit", "Initial Text", 0, 0, 200, 50, readonly=True
    )

    mock_dlg_model.createInstance.assert_called_once_with("com.sun.star.awt.UnoControlEditModel")
    assert edit.Name == "TestEdit"
    assert edit.PositionX == 0
    assert edit.PositionY == 0
    assert edit.Width == 200
    assert edit.Height == 50
    assert edit.Text == "Initial Text"
    assert edit.ReadOnly is True
    mock_dlg_model.insertByName.assert_called_once_with("TestEdit", mock_edit)

@patch("plugin.framework.dialogs._")
@patch("plugin.framework.i18n._")
def test_add_dialog_hyperlink(mock_i18n_translate, mock_translate):
    mock_translate.side_effect = lambda x: f"T_{x}"
    mock_i18n_translate.side_effect = lambda x: f"T_{x}"
    mock_dlg_model = MagicMock()
    mock_link = MagicMock()
    mock_dlg_model.createInstance.return_value = mock_link

    link = add_dialog_hyperlink(
        mock_dlg_model, "TestLink", "Click Link", "http://example.com", 2, 4, 10, 20
    )

    mock_dlg_model.createInstance.assert_called_once_with("com.sun.star.awt.UnoControlFixedHyperlinkModel")
    assert link.Name == "TestLink"
    assert link.PositionX == 2
    assert link.PositionY == 4
    assert link.Width == 10
    assert link.Height == 20
    assert link.Label == "T_Click Link"
    assert link.URL == "http://example.com"
    assert link.TextColor == 0x0563C1
    mock_dlg_model.insertByName.assert_called_once_with("TestLink", mock_link)

@patch("plugin.framework.i18n._")
def test_translate_dialog_xcc(mock_i18n_translate):
    mock_i18n_translate.side_effect = lambda x: f"T_{x}"

    # Setup a fake dialog with an XControlContainer that returns a child control
    mock_dlg = MagicMock()
    mock_xcc = MagicMock()
    mock_dlg.queryInterface.return_value = mock_xcc
    # Realistic impl name: Dialog is not in control_types, so translate_one reaches _xcc + children.
    mock_dlg.getImplementationName.return_value = "stardiv.Toolkit.UnoControlDialog"

    mock_child = MagicMock()
    mock_child.getImplementationName.return_value = "stardiv.Toolkit.UnoButtonControl"
    mock_child_model = MagicMock()
    mock_child_model.Name = "Btn1"
    mock_child_model.Label = "Old Label"
    mock_child.getModel.return_value = mock_child_model
    # Child doesn't have an XControlContainer itself
    mock_child.queryInterface.side_effect = Exception("No container")

    mock_xcc.getControls.return_value = [mock_child]

    # In Python 3, translate_dialog does `from plugin.framework.i18n import _` inside
    # the function.  Patching `plugin.framework.i18n._` is sufficient for this case
    # if it's imported at runtime. Let's see if this works!
    translate_dialog(mock_dlg)

    # Label should be updated
    assert mock_child_model.Label == "T_Old Label"
    # Ensure it traversed. It gets called twice:
    # 1. to check root_child_count
    # 2. in translate_one(dlg) to loop through children
    assert mock_xcc.getControls.call_count == 2
    mock_child.getImplementationName.assert_called_once()

@patch("plugin.framework.i18n._")
def test_translate_dialog_element_names(mock_i18n_translate):
    mock_i18n_translate.side_effect = lambda x: f"T_{x}"

    # Setup a fake dialog without XControlContainer, but with ElementNames
    mock_dlg = MagicMock()
    mock_dlg.queryInterface.side_effect = Exception("No container")

    mock_dlg_model = MagicMock()
    mock_dlg_model.ElementNames = ["Btn2"]
    mock_dlg.getModel.return_value = mock_dlg_model

    mock_child = MagicMock()
    mock_child.getImplementationName.return_value = "stardiv.Toolkit.UnoButtonControl"
    mock_child_model = MagicMock()
    mock_child_model.Name = "Btn2"
    mock_child_model.Label = "Another Label"
    mock_child.getModel.return_value = mock_child_model
    mock_child.queryInterface.side_effect = Exception("No container")

    mock_dlg.getControl.return_value = mock_child

    translate_dialog(mock_dlg)

    mock_dlg.getControl.assert_called_once_with("Btn2")
    assert mock_child_model.Label == "T_Another Label"

@patch("plugin.framework.i18n._")
def test_translate_dialog_listbox(mock_i18n_translate):
    mock_i18n_translate.side_effect = lambda x: f"T_{x}" if x else x

    mock_dlg = MagicMock()
    mock_dlg.queryInterface.side_effect = Exception("No container")

    mock_dlg_model = MagicMock()
    mock_dlg_model.ElementNames = ["List1"]
    mock_dlg.getModel.return_value = mock_dlg_model

    mock_child = MagicMock()
    mock_child.getImplementationName.return_value = "stardiv.Toolkit.UnoListBoxControl"
    mock_child_model = MagicMock()
    mock_child_model.Name = "List1"
    mock_child.getModel.return_value = mock_child_model

    mock_child.getStringItemList.return_value = ("Item1", "", "Item2")

    mock_dlg.getControl.return_value = mock_child

    translate_dialog(mock_dlg)

    mock_child.getStringItemList.assert_called_once()
    mock_child.setStringItemList.assert_called_once_with(("T_Item1", "", "T_Item2"))
