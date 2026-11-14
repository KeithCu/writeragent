
from unittest.mock import MagicMock

import pytest

from plugin.doc.compound_undo import UndoStepOrRollback, WriterCompoundUndo, _is_undo_locked, undo_titles
from plugin.framework.errors import DocumentDisposedError
from plugin.tests.testing_utils import FakeUndoManager


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


def _doc_with_undo(undo_mgr):
    doc = MagicMock()
    doc.getUndoManager.return_value = undo_mgr
    return doc


def test_undo_step_or_rollback_undoes_own_group_on_error():
    fake = FakeUndoManager(titles=["Earlier"], drop_empty=True)
    doc = _doc_with_undo(fake)
    step = UndoStepOrRollback(doc, "WriterAgent: test")

    with pytest.raises(RuntimeError, match="boom"):
        with step:
            fake.record()
            raise RuntimeError("boom")

    assert step.rolled_back is True
    assert fake.undone == 1
    assert fake.titles == ["Earlier"]


def test_undo_step_or_rollback_keeps_group_on_success():
    fake = FakeUndoManager(titles=["Earlier"], drop_empty=True)
    doc = _doc_with_undo(fake)
    step = UndoStepOrRollback(doc, "WriterAgent: test")

    with step:
        fake.record()

    assert step.rolled_back is False
    assert fake.undone == 0
    assert fake.titles == ["WriterAgent: test", "Earlier"]


def test_undo_step_or_rollback_empty_group_keeps_older_same_title():
    fake = FakeUndoManager(titles=["WriterAgent: test"], drop_empty=True)
    doc = _doc_with_undo(fake)
    step = UndoStepOrRollback(doc, "WriterAgent: test")

    with pytest.raises(RuntimeError, match="boom"):
        with step:
            raise RuntimeError("boom")

    assert step.rolled_back is False
    assert fake.undone == 0
    assert fake.titles == ["WriterAgent: test"]


def test_undo_step_or_rollback_leaves_foreign_top_entry():
    class _ForeignLeaveFake(FakeUndoManager):
        def leaveUndoContext(self) -> None:
            super().leaveUndoContext()
            self.titles.insert(0, "User edit")

    fake = _ForeignLeaveFake()
    doc = _doc_with_undo(fake)
    step = UndoStepOrRollback(doc, "WriterAgent: test")

    with pytest.raises(RuntimeError, match="boom"):
        with step:
            raise RuntimeError("boom")

    assert step.rolled_back is False
    assert fake.undone == 0
    assert fake.titles[0] == "User edit"


def test_undo_step_or_rollback_rolls_back_at_full_stack():
    fake = FakeUndoManager(titles=["c", "b", "a"], limit=3, drop_empty=True)
    doc = _doc_with_undo(fake)
    step = UndoStepOrRollback(doc, "WriterAgent: test")

    with pytest.raises(RuntimeError, match="boom"):
        with step:
            fake.record()
            raise RuntimeError("boom")

    assert step.rolled_back is True
    assert fake.undone == 1
    assert fake.titles == ["c", "b"]


def test_undo_step_or_rollback_skips_rollback_on_disposal():
    fake = FakeUndoManager()
    doc = _doc_with_undo(fake)
    step = UndoStepOrRollback(doc, "WriterAgent: test")

    with pytest.raises(_DisposedException):
        with step:
            raise _DisposedException("gone")

    assert step.rolled_back is False
    assert fake.undone == 0


def test_undo_step_or_rollback_locked_runs_body_without_group():
    fake = FakeUndoManager(locked=True)
    doc = _doc_with_undo(fake)
    ran = []
    step = UndoStepOrRollback(doc, "WriterAgent: test")

    with step:
        ran.append("ok")

    assert ran == ["ok"]
    assert step.entered is False
    assert step.rolled_back is False
    assert fake.entered == []
    assert fake.undone == 0


def _mock_undo_doc(*, titles_seq=None, locked=False):
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = locked
    if titles_seq is not None:
        mgr.getAllUndoActionTitles.side_effect = titles_seq
    doc.getUndoManager.return_value = mgr
    return doc, mgr


def test_commit_or_rollback_success():
    from plugin.doc.compound_undo import UndoOutcome, commit_or_rollback

    doc, mgr = _mock_undo_doc()
    called = []
    outcome = commit_or_rollback(doc, "Title", lambda: called.append(1))
    assert outcome == UndoOutcome()
    assert called == [1]
    mgr.enterUndoContext.assert_called_once_with("Title")
    mgr.leaveUndoContext.assert_called_once()
    mgr.undo.assert_not_called()


def test_commit_or_rollback_locked_or_missing_raises_undo_unavailable():
    from plugin.doc.compound_undo import UndoUnavailable, commit_or_rollback

    doc1 = MagicMock()
    doc1.getUndoManager.return_value = None
    with pytest.raises(UndoUnavailable):
        commit_or_rollback(doc1, "Title", lambda: None)

    doc2, mgr = _mock_undo_doc(locked=True)
    with pytest.raises(UndoUnavailable):
        commit_or_rollback(doc2, "Title", lambda: None)
    mgr.enterUndoContext.assert_not_called()


def test_commit_or_rollback_missing_islocked_enters():
    from plugin.doc.compound_undo import UndoOutcome, commit_or_rollback

    class _MinimalUndoManager:
        def __init__(self):
            self.entered = False

        def enterUndoContext(self, title: str) -> None:
            self.entered = True
            self.title = title

        def leaveUndoContext(self) -> None:
            pass

        def getAllUndoActionTitles(self):
            return ()

    mgr = _MinimalUndoManager()
    doc = MagicMock()
    doc.getUndoManager.return_value = mgr

    called = []
    outcome = commit_or_rollback(doc, "Title", lambda: called.append(1))
    assert outcome == UndoOutcome()
    assert called == [1]
    assert mgr.entered is True


def test_commit_or_rollback_magicmock_islocked_enters_and_mutates():
    from plugin.doc.compound_undo import UndoOutcome, commit_or_rollback

    doc = MagicMock()
    mgr = MagicMock()
    assert mgr.isLocked() is not True
    doc.getUndoManager.return_value = mgr

    called = []
    outcome = commit_or_rollback(doc, "Title", lambda: called.append(1))
    assert outcome == UndoOutcome()
    assert called == [1]
    mgr.enterUndoContext.assert_called_once_with("Title")


def test_commit_or_rollback_error_dict_rolls_back():
    from plugin.doc.compound_undo import commit_or_rollback

    doc, mgr = _mock_undo_doc(titles_seq=[(), ("Title",)])
    err = {"status": "error", "message": "boom"}
    outcome = commit_or_rollback(doc, "Title", lambda: err)
    assert outcome.error is err
    assert outcome.rolled_back is True
    mgr.leaveUndoContext.assert_called_once()
    mgr.undo.assert_called_once()


def test_commit_or_rollback_exception_rolls_back_and_returns_cause():
    from plugin.doc.compound_undo import commit_or_rollback

    doc, mgr = _mock_undo_doc(titles_seq=[(), ("Title",)])

    def mutate():
        raise ValueError("mutation error")

    outcome = commit_or_rollback(doc, "Title", mutate)
    assert isinstance(outcome.mutate_error, ValueError)
    assert str(outcome.mutate_error) == "mutation error"
    assert outcome.rolled_back is True
    mgr.leaveUndoContext.assert_called_once()
    mgr.undo.assert_called_once()


def test_commit_or_rollback_undo_failure_keeps_cause():
    from plugin.doc.compound_undo import commit_or_rollback

    doc, mgr = _mock_undo_doc(titles_seq=[(), ("Title",)])
    mgr.undo.side_effect = RuntimeError("undo failed")

    def mutate():
        raise ValueError("original cause")

    outcome = commit_or_rollback(doc, "Title", mutate)
    assert outcome.error is not None
    assert outcome.error["status"] == "error"
    assert outcome.error["code"] == "ROLLBACK_FAILED"
    assert "undo failed" in outcome.error["message"]
    assert "original cause" in outcome.error["message"]
    assert outcome.mutate_error is None
    assert outcome.rolled_back is False


def test_commit_or_rollback_does_not_undo_previous_same_title_action():
    from plugin.doc.compound_undo import commit_or_rollback

    doc, mgr = _mock_undo_doc(titles_seq=[("Title",), ("Title",)])
    session = MagicMock()
    session.changes = [1, 2]
    discard_calls: list[int] = []
    session.discard_changes_since.side_effect = lambda n: discard_calls.append(n)

    def mutate():
        raise ValueError("failed before write")

    outcome = commit_or_rollback(doc, "Title", mutate, session=session)
    assert isinstance(outcome.mutate_error, ValueError)
    assert str(outcome.mutate_error) == "failed before write"
    assert outcome.rolled_back is False
    assert discard_calls == []
    mgr.leaveUndoContext.assert_called_once()
    mgr.undo.assert_not_called()


def test_undo_outcome_raise_mutate_error():
    from plugin.doc.compound_undo import UndoOutcome

    UndoOutcome().raise_mutate_error()
    with pytest.raises(ValueError, match="x"):
        UndoOutcome(mutate_error=ValueError("x")).raise_mutate_error()


def test_commit_or_rollback_failed_leave_after_success_is_an_error():
    from plugin.doc.compound_undo import commit_or_rollback

    doc, mgr = _mock_undo_doc()
    mgr.leaveUndoContext.side_effect = RuntimeError("leave failed")

    outcome = commit_or_rollback(doc, "Title", lambda: None)
    assert outcome.error is not None
    assert outcome.error["status"] == "error"
    assert outcome.error["code"] == "UNDO_CONTEXT_LEAK"
    mgr.undo.assert_not_called()


def test_commit_or_rollback_disposal_skips_rollback():
    from plugin.doc.compound_undo import commit_or_rollback
    from plugin.framework.errors import DocumentDisposedError

    doc, mgr = _mock_undo_doc()

    def mutate():
        raise DocumentDisposedError("gone")

    with pytest.raises(DocumentDisposedError):
        commit_or_rollback(doc, "Title", mutate)
    mgr.undo.assert_not_called()
    mgr.leaveUndoContext.assert_called_once()


def test_commit_or_rollback_session_discard_runs_after_undo():
    from plugin.doc.compound_undo import commit_or_rollback

    doc, mgr = _mock_undo_doc(titles_seq=[(), ("Title",)])
    session = MagicMock()
    session.changes = [1, 2, 3]
    discard_calls: list[int] = []
    session.discard_changes_since.side_effect = lambda n: discard_calls.append(n)

    def mutate():
        raise ValueError("boom")

    commit_or_rollback(doc, "Title", mutate, session=session)
    assert discard_calls == [3]
    assert session.discard_changes_since.call_count == 1


def test_commit_or_rollback_session_discard_error_is_swallowed():
    from plugin.doc.compound_undo import commit_or_rollback

    doc, mgr = _mock_undo_doc(titles_seq=[(), ("Title",)])
    session = MagicMock()
    session.changes = [1]
    session.discard_changes_since.side_effect = RuntimeError("discard failed")

    def mutate():
        raise ValueError("original")

    outcome = commit_or_rollback(doc, "Title", mutate, session=session)
    assert isinstance(outcome.mutate_error, ValueError)
    assert str(outcome.mutate_error) == "original"
    assert outcome.rolled_back is True
    assert outcome.nothing_landed is True
    mgr.undo.assert_called_once()


def test_nothing_landed_follows_undo_or_unchanged_session():
    from plugin.doc.compound_undo import commit_or_rollback

    doc, mgr = _mock_undo_doc(titles_seq=[(), ("Title",)])
    outcome = commit_or_rollback(doc, "Title", lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert outcome.rolled_back is True
    assert outcome.nothing_landed is True

    session = MagicMock()
    session.changes = [1]
    mgr.reset_mock()
    mgr.getAllUndoActionTitles.side_effect = [(), ("Title",)]
    outcome2 = commit_or_rollback(doc, "Title", lambda: session.changes.append(2), session=session)
    assert outcome2.rolled_back is False
    assert outcome2.nothing_landed is False

    session.changes = [1]
    mgr.reset_mock()
    mgr.getAllUndoActionTitles.side_effect = [("Title",), ("Title",)]
    outcome3 = commit_or_rollback(doc, "Title", lambda: None, session=session)
    assert outcome3.rolled_back is False
    assert outcome3.nothing_landed is True
