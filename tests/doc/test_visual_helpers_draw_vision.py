# WriterAgent - SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

from plugin.doc.visual_helpers import graphic_objects_in_selection


def test_draw_unnamed_selected_graphic_in_selection():
    graphic = MagicMock()
    doc = MagicMock()
    with patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="draw"), patch(
        "plugin.doc.visual_helpers.selected_graphic_object", return_value=graphic
    ), patch("plugin.doc.visual_helpers._graphic_object_name", return_value=""):
        pairs = graphic_objects_in_selection(doc)
    assert pairs == [("", graphic)]
