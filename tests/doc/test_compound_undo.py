
import pytest

from plugin.doc.compound_undo import WriterCompoundUndo
from plugin.framework.errors import DocumentDisposedError
import plugin.writer.edit_review as edit_review


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


def test_edit_review_has_no_writer_compound_undo_attribute():
    assert not hasattr(edit_review, "WriterCompoundUndo")


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
