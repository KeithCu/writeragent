
from unittest.mock import MagicMock

import pytest

from plugin.doc.compound_undo import WriterCompoundUndo, _is_undo_locked, undo_titles
from plugin.framework.errors import DocumentDisposedError


class _MockUndoManager:
    def __init__(self):
        self.entered = False
        self.left = False
        self.undone = False
        self.titles = []
        self.discard_empty_context = False
        self.locked = False

    def isLocked(self) -> bool:
        return self.locked

    def enterUndoContext(self, title: str) -> None:
        self.entered = True
        self.current_title = title
        assert "WriterAgent" in title

    def leaveUndoContext(self) -> None:
        self.left = True
        if hasattr(self, "current_title"):
            if not self.discard_empty_context:
                self.titles.insert(0, self.current_title)
            del self.current_title

    def getAllUndoActionTitles(self):
        return tuple(self.titles)

    def undo(self) -> None:
        self.undone = True
        if self.titles:
            self.titles.pop(0)


class _MockDoc:
    def __init__(self, recording=True):
        self.props = {"RecordChanges": recording}
        self.writes = []
        self.undo = _MockUndoManager()

    def getPropertyValue(self, name):
        return self.props[name]

    def setPropertyValue(self, name, value):
        self.writes.append((name, value))
        self.props[name] = value

    def getUndoManager(self):
        return self.undo


def test_writer_compound_undo_enter_close_and_idempotent():
    doc = _MockDoc(recording=True)
    cu = WriterCompoundUndo(doc, "WriterAgent: test")
    assert doc.undo.entered is True
    assert doc.undo.left is False
    cu.close()
    assert doc.undo.left is True
    cu.close()
    assert doc.undo.left is True


def test_writer_compound_undo_context_manager_closes_on_success_and_error():
    doc = _MockDoc(recording=True)
    with WriterCompoundUndo(doc, "WriterAgent: with-ok") as cu:
        assert cu is not None
        assert doc.undo.entered is True
        assert doc.undo.left is False
    assert doc.undo.left is True

    doc2 = _MockDoc(recording=True)
    try:
        with WriterCompoundUndo(doc2, "WriterAgent: with-err"):
            assert doc2.undo.entered is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert doc2.undo.left is True


def test_writer_compound_undo_locked_manager_never_enters_or_leaves():
    doc = _MockDoc()
    doc.undo.locked = True
    cu = WriterCompoundUndo(doc, "WriterAgent: locked")
    assert cu.entered is False
    assert doc.undo.entered is False
    cu.close()
    assert doc.undo.left is False


class _DisposedException(Exception):
    pass


def test_writer_compound_undo_reraises_disposal_from_enter():
    class _DisposedDoc:
        def getUndoManager(self):
            raise _DisposedException("gone")

    with pytest.raises(DocumentDisposedError):
        WriterCompoundUndo(_DisposedDoc(), "WriterAgent: disposed")


def test_writer_compound_undo_reraises_disposal_from_leave():
    doc = _MockDoc()

    def _leave():
        raise _DisposedException("gone")

    setattr(doc.undo, "leaveUndoContext", _leave)
    cu = WriterCompoundUndo(doc, "WriterAgent: disposed")
    with pytest.raises(DocumentDisposedError):
        cu.close()


class _FlakyLeaveUndoManager(_MockUndoManager):
    def __init__(self):
        super().__init__()
        self.fail_leave = 1

    def leaveUndoContext(self):
        if self.fail_leave:
            self.fail_leave -= 1
            raise RuntimeError("leave failed")
        super().leaveUndoContext()


def test_writer_compound_undo_close_can_retry_after_failed_leave():
    doc = _MockDoc()
    doc.undo = _FlakyLeaveUndoManager()
    cu = WriterCompoundUndo(doc, "WriterAgent: flaky")
    assert cu.close() is False
    assert doc.undo.left is False
    assert cu.close() is True
    assert doc.undo.left is True


def test_is_undo_locked_only_is_true_counts_as_locked():
    """PyUNO returns bool; test doubles may return other truthy values — only ``True`` locks."""

    class _Locked:
        def isLocked(self):
            return True

    class _UnlockedBool:
        def isLocked(self):
            return False

    class _UnlockedInt:
        def isLocked(self):
            return 1

    class _NoIsLocked:
        pass

    assert _is_undo_locked(_Locked()) is True
    assert _is_undo_locked(_UnlockedBool()) is False
    assert _is_undo_locked(_UnlockedInt()) is False
    assert _is_undo_locked(_NoIsLocked()) is False


def test_writer_compound_undo_enters_when_islocked_returns_one():
    doc = _MockDoc(recording=True)

    def _locked_one():
        return 1

    setattr(doc.undo, "isLocked", _locked_one)
    cu = WriterCompoundUndo(doc, "WriterAgent: truthy-not-true")
    assert cu.entered is True
    assert doc.undo.entered is True
    cu.close()


def test_writer_compound_undo_magicmock_islocked_enters():
    from unittest.mock import MagicMock
    doc = MagicMock()
    mgr = MagicMock()
    assert mgr.isLocked() is not True
    doc.getUndoManager.return_value = mgr
    cu = WriterCompoundUndo(doc, "WriterAgent: mock")
    assert cu.entered is True
    mgr.enterUndoContext.assert_called_once_with("WriterAgent: mock")
    cu.close()


def test_writer_compound_undo_try_enter_returns_none_when_unavailable():
    from unittest.mock import MagicMock
    # Locked
    doc_locked = MagicMock()
    doc_locked.getUndoManager.return_value.isLocked.return_value = True
    assert WriterCompoundUndo.try_enter(doc_locked, "WriterAgent: locked") is None

    # None manager
    doc_none = MagicMock()
    doc_none.getUndoManager.return_value = None
    assert WriterCompoundUndo.try_enter(doc_none, "WriterAgent: none") is None

    # Missing getUndoManager
    doc_missing = object()
    assert WriterCompoundUndo.try_enter(doc_missing, "WriterAgent: missing") is None

    # Non-disposal enter error
    doc_err = MagicMock()
    doc_err.getUndoManager.return_value.isLocked.return_value = False
    doc_err.getUndoManager.return_value.enterUndoContext.side_effect = RuntimeError("enter failed")
    assert WriterCompoundUndo.try_enter(doc_err, "WriterAgent: err") is None


def test_writer_compound_undo_try_enter_reraises_disposal():
    from unittest.mock import MagicMock
    doc_disp = MagicMock()
    doc_disp.getUndoManager.side_effect = _DisposedException("doc disposed")
    with pytest.raises(DocumentDisposedError):
        WriterCompoundUndo.try_enter(doc_disp, "WriterAgent: disp")


def test_writer_compound_undo_rollback_if_top():
    from unittest.mock import MagicMock
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [(), ("WriterAgent: test",)]
    doc.getUndoManager.return_value = mgr

    cu = WriterCompoundUndo.try_enter(doc, "WriterAgent: test")
    assert cu is not None
    # Still open -> returns False, does not undo
    assert cu.rollback_if_top() is False
    mgr.undo.assert_not_called()

    # Closed -> top title matches and changed -> undoes
    assert cu.close() is True
    assert cu.rollback_if_top() is True
    mgr.undo.assert_called_once()

    # Never entered -> returns False
    cu_unentered = WriterCompoundUndo(object(), "WriterAgent: unentered")
    assert cu_unentered.entered is False
    assert cu_unentered.rollback_if_top() is False

    # Titles unchanged -> does not undo
    doc_unchanged = MagicMock()
    mgr2 = MagicMock()
    mgr2.isLocked.return_value = False
    mgr2.getAllUndoActionTitles.side_effect = [("WriterAgent: test",), ("WriterAgent: test",)]
    doc_unchanged.getUndoManager.return_value = mgr2
    cu_unchanged = WriterCompoundUndo.try_enter(doc_unchanged, "WriterAgent: test")
    assert cu_unchanged is not None
    assert cu_unchanged.close() is True
    assert cu_unchanged.rollback_if_top() is False
    mgr2.undo.assert_not_called()


def test_undo_titles_returns_tuple_newest_first():
    doc = _MockDoc()
    doc.undo.titles = ["second", "first"]
    assert undo_titles(doc) == ("second", "first")
    doc.undo.titles = []
    assert undo_titles(doc) == ()


def test_undo_titles_none_when_unreadable():
    assert undo_titles(object()) is None

    doc_none = MagicMock()
    doc_none.getUndoManager.return_value = None
    assert undo_titles(doc_none) is None

    doc_err = MagicMock()
    doc_err.getUndoManager.return_value.getAllUndoActionTitles.side_effect = RuntimeError("fail")
    assert undo_titles(doc_err) is None


def test_undo_titles_reraises_disposal():
    doc = MagicMock()
    doc.getUndoManager.return_value.getAllUndoActionTitles.side_effect = _DisposedException("gone")
    with pytest.raises(DocumentDisposedError):
        undo_titles(doc)


def test_writer_compound_undo_unreadable_titles_refuses_rollback():
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [RuntimeError("read failed"), ("WriterAgent: test",)]
    doc.getUndoManager.return_value = mgr

    cu = WriterCompoundUndo.try_enter(doc, "WriterAgent: test")
    assert cu is not None
    assert cu.entered is True
    assert cu._titles_before is None
    assert cu.close() is True
    assert cu.rollback_if_top() is False
    mgr.undo.assert_not_called()
