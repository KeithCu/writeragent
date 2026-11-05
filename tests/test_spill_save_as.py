from unittest.mock import MagicMock

from plugin.calc.python.spill import SpillCell, SpillRecord
from plugin.calc.python.workbook_state import WorkbookState


def test_spill_save_as_lifecycle_consistency():
    # If a document changes URL (e.g., Save As), its spill registry should still be
    # correctly accessed because it uses the lifecycle key (RuntimeUID).
    WorkbookState.reset_for_tests()
    mock_doc = MagicMock()
    mock_doc.getRuntimeUID.return_value = "uid-123"
    mock_doc.getPropertyValue.return_value = "uid-123"
    mock_doc.getURL.return_value = "file:///tmp/old.ods"

    state = WorkbookState.for_doc(mock_doc)
    assert state is not None
    assert state.key == "uid-123"

    state.spills[("Sheet1", 0, 0)] = SpillRecord(
        code="result = 1",
        cells=[SpillCell(1, 0, "n", 1.0), SpillCell(2, 0, "n", 2.0)],
    )

    # Save As simulates URL change, but RuntimeUID remains.
    mock_doc.getURL.return_value = "file:///tmp/new.ods"

    # Check if the registry still works with the new URL / same doc
    new_state = WorkbookState.for_doc(mock_doc)
    assert new_state is state
    assert ("Sheet1", 0, 0) in new_state.spills
    assert new_state.spills[("Sheet1", 0, 0)].spills == [(1, 0), (2, 0)]
