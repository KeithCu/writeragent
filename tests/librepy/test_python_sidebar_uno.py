# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for LibrePy Python sidebar list population."""

from __future__ import annotations

from typing import Any

from plugin.librepy.python_sidebar import _populate_listbox
from plugin.testing_runner import native_test


@native_test
def test_populate_listbox_string_item_list(ctx: Any) -> None:
    """Verify ListBox model StringItemList assignment populates items (S1 trim check)."""
    smgr = ctx.getServiceManager()
    ctrl = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlListBox", ctx)
    model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlListBoxModel", ctx)
    ctrl.setModel(model)

    items = ["Alpha", "Beta", "Gamma"]
    _populate_listbox(ctrl, items)
    assert list(model.StringItemList) == items


@native_test
def test_populate_combobox_string_item_list(ctx: Any) -> None:
    """Verify ComboBox model StringItemList assignment populates items."""
    smgr = ctx.getServiceManager()
    ctrl = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlComboBox", ctx)
    model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlComboBoxModel", ctx)
    ctrl.setModel(model)

    items = ["Option A", "Option B"]
    _populate_listbox(ctrl, items)
    assert list(model.StringItemList) == items
