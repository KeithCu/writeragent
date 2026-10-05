from unittest.mock import MagicMock, patch
from plugin.draw.bridge import DrawBridge

def test_calc_shapes_target_active_sheet_mock():
    """Verify that UpsertShape targets the active sheet and not sheet 0."""
    # Setup mock doc
    doc = MagicMock()
    doc.supportsService.return_value = True

    # Mock controller
    controller = MagicMock()
    doc.getCurrentController.return_value = controller

    # Mock sheets
    sheet0 = MagicMock()
    sheet0.getName.return_value = "Sheet1"
    sheet0_page = MagicMock()
    sheet0.getDrawPage.return_value = sheet0_page

    sheet1 = MagicMock()
    sheet1.getName.return_value = "Sheet2"
    sheet1_page = MagicMock()
    sheet1.getDrawPage.return_value = sheet1_page

    # setActiveSheet equivalent mock behavior
    controller.ActiveSheet = sheet1

    draw_pages = MagicMock()
    draw_pages.getCount.return_value = 2
    # Important for identity comparison uno_same
    def mock_get_by_index(i):
        if i == 0:
            return sheet0_page
        elif i == 1:
            return sheet1_page
        raise IndexError()
    draw_pages.getByIndex.side_effect = mock_get_by_index
    doc.getDrawPages.return_value = draw_pages

    # Bridge should correctly identify idx 1 because page is sheet1.getDrawPage()
    with patch('plugin.framework.uno_context.uno_same', side_effect=lambda a, b: a is b):
        bridge = DrawBridge(doc)
        idx = bridge.get_active_page_index()
        assert idx == 1
