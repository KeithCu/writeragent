# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Comment helper tests (list/read paths). No LibreOffice required."""
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch
from plugin.framework.errors import DocumentDisposedError
from plugin.writer.specialized.comments import CommentList


def test_read_annotation_falls_back_to_paragraph_context():
    from plugin.writer.specialized.comments import _read_annotation

    anchor = MagicMock()
    anchor.getString.return_value = ""
    field = MagicMock()
    field.getAnchor.return_value = anchor
    field.getPropertyValue.side_effect = lambda p: {
        "Author": "Rev",
        "Content": "note",
        "DateTimeValue": SimpleNamespace(Year=2026, Month=1, Day=2, Hours=3, Minutes=4),
    }.get(p, "")
    doc_svc = MagicMock()
    doc_svc.find_paragraph_for_range.return_value = 7
    with patch("plugin.writer.search.enclosing_paragraph_text", return_value="  the clause the comment covers  "):
        entry = _read_annotation(field, [], MagicMock(), doc_svc)
    assert entry["anchor_preview"] == "the clause the comment covers"
    assert entry["anchor_is_paragraph_context"] is True


def test_comment_workflow_split_tool_names():
    from plugin.writer.specialized import comments

    assert comments.CommentScanTasks.name == "comment_scan_tasks"
    assert comments.CommentWorkflowGet.name == "comment_workflow_get"
    assert comments.CommentWorkflowSet.name == "comment_workflow_set"
    assert comments.CommentCheckStop.name == "comment_check_stop"
    assert comments.CommentWorkflowSet.is_mutation is True
    assert comments.CommentScanTasks().detects_mutation() is False
    assert comments.CommentScanTasks().requires_document_lock() is False
    assert comments.CommentCheckStop().detects_mutation() is False
    assert comments.CommentCheckStop().requires_document_lock() is False
    assert not hasattr(comments, "CommentWorkflow")


def test_specialized_writer_tools_mutation_and_uno_services_contract():
    from plugin.writer.specialized import bookmarks, comments, fields, forms

    expected_mutations = {
        # Comments
        comments.CommentList: False,
        comments.CommentScanTasks: False,
        comments.CommentWorkflowGet: False,
        comments.CommentCheckStop: False,
        comments.AddComment: True,
        comments.CommentDelete: True,
        comments.CommentResolve: True,
        comments.CommentWorkflowSet: True,
        # Bookmarks
        bookmarks.BookmarkList: False,
        bookmarks.BookmarkGet: False,
        bookmarks.BookmarkResolve: False,
        bookmarks.BookmarkCleanup: True,
        bookmarks.BookmarkCreate: True,
        bookmarks.BookmarkDelete: True,
        bookmarks.BookmarkRename: True,
        # Fields
        fields.FieldsList: False,
        fields.FieldsUpdateAll: True,
        fields.FieldsDelete: True,
        fields.FieldsInsert: True,
        # Forms
        forms.FormListControls: False,
        forms.FormCreateControl: True,
        forms.FormCreate: True,
        forms.FormGenerate: True,
        forms.FormEditControl: True,
        forms.FormDeleteControl: True,
    }

    form_union = forms._FORM_DOC_SERVICES
    text_doc = ["com.sun.star.text.TextDocument"]

    for tool_cls, expected_mut in expected_mutations.items():
        tool = tool_cls()
        assert tool.detects_mutation() is expected_mut, f"{tool.name} detects_mutation should be {expected_mut}"
        if tool_cls in (forms.FormListControls, forms.FormCreateControl, forms.FormCreate, forms.FormGenerate, forms.FormEditControl, forms.FormDeleteControl):
            assert tool.uno_services == form_union, f"{tool.name} uno_services should be form union"
        else:
            assert tool.uno_services == text_doc, f"{tool.name} uno_services should be text_doc"


def test_workflow_task_prefixes_shared():
    from plugin.framework.constants import WORKFLOW_TASK_PREFIXES
    from plugin.writer.specialized.comments import CommentScanTasks, _WORKFLOW_TASK_PREFIXES
    from plugin.scripting import writeragent_api

    expected = ("TODO-AI", "FIX", "QUESTION", "VALIDATION", "NOTE")
    assert WORKFLOW_TASK_PREFIXES == expected
    assert _WORKFLOW_TASK_PREFIXES == WORKFLOW_TASK_PREFIXES
    assert writeragent_api.WORKFLOW_TASK_PREFIXES == WORKFLOW_TASK_PREFIXES

    # Schema consistency
    enum_values = CommentScanTasks.parameters["properties"]["prefix_filter"]["enum"]
    assert enum_values == list(WORKFLOW_TASK_PREFIXES)
    for prefix in WORKFLOW_TASK_PREFIXES:
        assert prefix in CommentScanTasks.description


def test_comment_scan_tasks_with_prefixes():
    from plugin.writer.specialized.comments import _comment_scan_tasks

    def make_field(content: str, resolved: bool = False):
        f = MagicMock()
        f.supportsService.return_value = True
        f.getPropertyValue.side_effect = lambda prop: {
            "Content": content,
            "Resolved": resolved,
            "Author": "Reviewer",
            "DateTimeValue": SimpleNamespace(Year=2026, Month=1, Day=2, Hours=3, Minutes=4),
        }.get(prop, "")
        anchor = MagicMock()
        anchor.getString.return_value = "Selected text"
        f.getAnchor.return_value = anchor
        return f

    f1 = make_field("TODO-AI: refactor this method", resolved=False)
    f2 = make_field("FIX: bug here", resolved=True)
    f3 = make_field("NOTE: general comment", resolved=False)
    f4 = make_field("Arbitrary comment", resolved=False)

    class FakeEnum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    doc = MagicMock()
    fields = MagicMock()
    fields.createEnumeration.side_effect = lambda: FakeEnum([f1, f2, f3, f4])
    doc.getTextFields.return_value = fields
    doc.getText.return_value = MagicMock()

    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_paragraph_ranges.return_value = []
    ctx.services.document.find_paragraph_for_range.return_value = None

    # Unresolved only (default)
    res = _comment_scan_tasks(ctx, {})
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert [t["prefix"] for t in res["tasks"]] == ["TODO-AI", "NOTE"]

    # Filter by specific prefix
    fields.createEnumeration.side_effect = lambda: FakeEnum([f1, f2, f3, f4])
    res_filtered = _comment_scan_tasks(ctx, {"prefix_filter": "NOTE"})
    assert res_filtered["status"] == "ok"
    assert res_filtered["count"] == 1
    assert res_filtered["tasks"][0]["prefix"] == "NOTE"


def test_add_comment_schema_parent_name_optional():
    from plugin.writer.specialized.comments import AddComment

    params = AddComment.parameters
    assert "parent_name" in params["properties"]
    assert params["required"] == ["content"]
    assert "search" not in params["required"]
    assert "parent_name" not in params["required"]


def test_add_comment_reply_unknown_parent():
    from plugin.writer.specialized.comments import AddComment

    class FakeEnum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    doc = MagicMock()
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: FakeEnum([])
    res = AddComment().execute(MagicMock(doc=doc), content="a reply", parent_name="no-such-comment")
    assert res["status"] == "error"
    assert res.get("comment_added") is False
    assert "no-such-comment" in res["message"]


def test_add_comment_reply_via_parent_name_skips_search():
    """parent_name is the reply path: no text search, ParentName = immediate parent, not Resolved."""
    from plugin.writer.specialized.comments import AddComment

    class FakeEnum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    parent = MagicMock()
    parent.supportsService.return_value = True
    # B is itself a reply to A — we must parent the new comment to B, not walk to A.
    parent.getPropertyValue.side_effect = lambda p: {
        "Name": "B",
        "ParentName": "A",
        "Resolved": False,
    }.get(p, "")

    reply = MagicMock()
    reply.supportsService.return_value = True
    reply.getPropertyValue.side_effect = lambda p: {
        "Name": "C",
        "ParentName": "B",
    }.get(p, "")

    doc = MagicMock()
    doc.createInstance.return_value = reply
    calls = {"n": 0}

    def _enum():
        calls["n"] += 1
        if calls["n"] <= 2:
            return FakeEnum([parent])
        return FakeEnum([parent, reply])

    doc.getTextFields.return_value.createEnumeration.side_effect = _enum
    doc_text = MagicMock()
    doc.getText.return_value = doc_text
    host = MagicMock()
    parent.getAnchor.return_value.getText.return_value = host

    with patch("plugin.writer.specialized.comments._set_annotation_date"):
        res = AddComment().execute(
            MagicMock(doc=doc),
            content="approved",
            parent_name="B",
            search="should-be-ignored",
        )
    assert res["status"] == "ok", res
    assert res["name"] == "C"
    assert res["parent_name"] == "B"
    assert res["comment_added"] is True
    doc.findFirst.assert_not_called()
    reply.setPropertyValue.assert_any_call("ParentName", "B")
    assert not any(c[0][0] == "Resolved" for c in reply.setPropertyValue.call_args_list)
    parent.setPropertyValue.assert_not_called()
    host.insertTextContent.assert_called_once()
    _cursor, _field, absorb = host.insertTextContent.call_args[0]
    assert absorb is False
    doc_text.insertTextContent.assert_not_called()


def test_ensure_annotation_name_assigns_when_empty():
    from plugin.writer.specialized.comments import _ensure_annotation_name

    class FakeEnum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    existing = MagicMock()
    existing.supportsService.return_value = True
    existing.getPropertyValue.return_value = "__Annotation__1_1"
    field = MagicMock()
    field.getPropertyValue.return_value = ""
    doc = MagicMock()
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: FakeEnum([existing])
    name = _ensure_annotation_name(field, doc)
    assert name.startswith("__Annotation__")
    assert name != "__Annotation__1_1"
    field.setPropertyValue.assert_called_once_with("Name", name)


def test_name_of_new_annotation_reads_back_from_doc():
    """Point-insert replies leave Name empty on the instance; read the new Name from the doc."""
    from plugin.writer.specialized.comments import _name_of_new_annotation

    class FakeEnum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    field = MagicMock()
    field.getPropertyValue.return_value = ""
    listed = MagicMock()
    listed.supportsService.return_value = True
    listed.getPropertyValue.return_value = "__Annotation__9_1"
    doc = MagicMock()
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: FakeEnum([listed])
    assert _name_of_new_annotation(doc, field, names_before=[]) == "__Annotation__9_1"

# ---- D4: add_comment span / occurrence / author -----------------------------

class _FiniteEnum:
    def __init__(self, items):
        self._items = list(items)

    def hasMoreElements(self):
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


def _fields_that_grow():
    """getTextFields mock: empty on first enumeration, one Annotation after insert."""
    calls = {"n": 0}
    field = MagicMock()
    field.supportsService.return_value = True
    fields = MagicMock()

    def _enum():
        calls["n"] += 1
        return _FiniteEnum([] if calls["n"] == 1 else [field])

    fields.createEnumeration.side_effect = _enum
    return fields


def test_add_comment_occurrence_author_and_span():
    from plugin.writer.specialized.comments import AddComment

    doc = MagicMock()
    first, second = MagicMock(), MagicMock()
    second.getString.return_value = "second hit"
    second.getText.return_value = mtext = MagicMock()
    cursor = MagicMock()
    mtext.createTextCursorByRange.return_value = cursor
    doc.findFirst.return_value = first
    doc.findNext.return_value = second
    doc.getTextFields.return_value = _fields_that_grow()
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.writer.specialized.comments._set_annotation_date"):
        res = AddComment().execute(ctx, content="note", search="hit", occurrence=1, author="Rev")
    assert res["status"] == "ok" and res["author"] == "Rev" and res["anchor_text"] == "second hit"
    # Assert spanning-anchor insert: cursor covers found, absorb=True.
    mtext.createTextCursorByRange.assert_called_once_with(second.getStart.return_value)
    cursor.gotoRange.assert_called_once_with(second.getEnd.return_value, True)
    mtext.insertTextContent.assert_called_once_with(cursor, doc.createInstance.return_value, True)


def test_add_comment_errors_when_annotation_does_not_register():
    from plugin.writer.specialized.comments import AddComment

    doc = MagicMock()
    found = MagicMock()
    found.getString.return_value = "hit"
    found.getText.return_value = MagicMock()
    doc.findFirst.return_value = found
    empty = MagicMock()
    empty.createEnumeration.side_effect = lambda: _FiniteEnum([])
    doc.getTextFields.return_value = empty
    res = AddComment().execute(SimpleNamespace(doc=doc), content="note", search="hit")
    assert res["status"] == "error" and res["comment_added"] is False and res["matched"] is True


def test_add_comment_not_found_at_occurrence():
    from plugin.writer.specialized.comments import AddComment

    doc = MagicMock()
    doc.findFirst.return_value = MagicMock()
    doc.findNext.return_value = None
    res = AddComment().execute(SimpleNamespace(doc=doc), content="n", search="x", occurrence=3)
    assert res["status"] == "error" and res["comment_added"] is False


# ---- 4) delete_comment miss is an error ---------------------------------------

def test_delete_comment_not_found_is_error():
    from plugin.writer.specialized.comments import CommentDelete

    doc = MagicMock()
    doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    ctx = MagicMock()
    ctx.doc = doc
    res = CommentDelete().execute(ctx, name="nope")
    assert res["status"] == "error" and res["code"] == "COMMENT_NOT_FOUND"
    assert res["deleted"] == 0
    assert "comment_list" in res["message"]


def _make_comment_field(name: str, parent: str = "", author: str = "", host: Any = None):
    field = MagicMock()
    field.supportsService.return_value = True
    field.getPropertyValue.side_effect = lambda prop: {
        "Name": name,
        "ParentName": parent,
        "Author": author,
    }.get(prop, "")
    if host is not None:
        field.getAnchor.return_value.getText.return_value = host
    return field


def _setup_mock_undo_manager(doc: Any) -> MagicMock:
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    entered: dict[str, str] = {}
    mgr.enterUndoContext.side_effect = lambda t: entered.__setitem__("title", t)
    mgr.getAllUndoActionTitles.side_effect = lambda: (entered.get("title"),) if "title" in entered else ()
    doc.getUndoManager.return_value = mgr
    return mgr


def test_delete_comment_by_name_removes_thread_chain():
    from plugin.writer.specialized.comments import CommentDelete

    doc = MagicMock()
    _setup_mock_undo_manager(doc)
    text_obj = MagicMock()
    doc.getText.return_value = text_obj
    host = MagicMock()

    field_a = _make_comment_field("A", "", "AuthorA", host=host)
    field_b = _make_comment_field("B", "A", "AuthorB", host=host)
    field_c = _make_comment_field("C", "B", "AuthorC", host=host)
    field_d = _make_comment_field("D", "", "AuthorD", host=host)

    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum(
        [field_a, field_b, field_c, field_d]
    )
    ctx = MagicMock(doc=doc)

    res = CommentDelete().execute(ctx, name="A")
    assert res == {"status": "ok", "deleted": 3}
    assert host.removeTextContent.call_args_list == [
        call(field_a),
        call(field_b),
        call(field_c),
    ]
    text_obj.removeTextContent.assert_not_called()


def test_delete_comment_by_name_removes_subthread_only():
    from plugin.writer.specialized.comments import CommentDelete

    doc = MagicMock()
    _setup_mock_undo_manager(doc)
    text_obj = MagicMock()
    doc.getText.return_value = text_obj
    host = MagicMock()

    field_a = _make_comment_field("A", "", "AuthorA", host=host)
    field_b = _make_comment_field("B", "A", "AuthorB", host=host)
    field_c = _make_comment_field("C", "B", "AuthorC", host=host)
    field_d = _make_comment_field("D", "", "AuthorD", host=host)

    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum(
        [field_a, field_b, field_c, field_d]
    )
    ctx = MagicMock(doc=doc)

    res = CommentDelete().execute(ctx, name="B")
    assert res == {"status": "ok", "deleted": 2}
    assert host.removeTextContent.call_args_list == [
        call(field_b),
        call(field_c),
    ]
    text_obj.removeTextContent.assert_not_called()


def test_delete_comment_blank_name_reply_removed_and_other_root_stays():
    from plugin.writer.specialized.comments import CommentDelete

    doc = MagicMock()
    _setup_mock_undo_manager(doc)
    text_obj = MagicMock()
    doc.getText.return_value = text_obj
    host = MagicMock()

    field_a = _make_comment_field("A", "", "AuthorA", host=host)
    field_blank = _make_comment_field("", "A", "AuthorBlank", host=host)
    field_e = _make_comment_field("E", "", "AuthorE", host=host)

    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum(
        [field_a, field_blank, field_e]
    )
    ctx = MagicMock(doc=doc)

    res = CommentDelete().execute(ctx, name="A")
    assert res == {"status": "ok", "deleted": 2}
    assert host.removeTextContent.call_args_list == [
        call(field_a),
        call(field_blank),
    ]
    text_obj.removeTextContent.assert_not_called()


def test_delete_comment_by_author_removes_matching_author_only():
    from plugin.writer.specialized.comments import CommentDelete

    doc = MagicMock()
    _setup_mock_undo_manager(doc)
    text_obj = MagicMock()
    doc.getText.return_value = text_obj
    host = MagicMock()

    field_root = _make_comment_field("Root", "", "Z", host=host)
    field_reply = _make_comment_field("Reply", "Root", "Other", host=host)

    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum(
        [field_root, field_reply]
    )
    ctx = MagicMock(doc=doc)

    res = CommentDelete().execute(ctx, author="Z")
    assert res == {"status": "ok", "deleted": 1}
    assert host.removeTextContent.call_args_list == [
        call(field_root),
    ]
    text_obj.removeTextContent.assert_not_called()


def test_delete_comment_name_and_author_union_each_field_once():
    from plugin.writer.specialized.comments import CommentDelete

    doc = MagicMock()
    _setup_mock_undo_manager(doc)
    text_obj = MagicMock()
    doc.getText.return_value = text_obj
    host = MagicMock()

    field_a = _make_comment_field("A", "", "Other", host=host)
    field_b = _make_comment_field("B", "A", "Z", host=host)
    field_other_z = _make_comment_field("OtherZ", "", "Z", host=host)
    field_d = _make_comment_field("D", "", "Other", host=host)

    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum(
        [field_a, field_b, field_other_z, field_d]
    )
    ctx = MagicMock(doc=doc)

    res = CommentDelete().execute(ctx, name="A", author="Z")
    assert res == {"status": "ok", "deleted": 3}
    assert host.removeTextContent.call_args_list == [
        call(field_a),
        call(field_b),
        call(field_other_z),
    ]
    text_obj.removeTextContent.assert_not_called()


def test_comment_resolve_miss_returns_error():
    from plugin.writer.specialized.comments import CommentResolve

    doc = MagicMock()
    doc.getTextFields.return_value.createEnumeration.return_value = _FiniteEnum([])
    ctx = MagicMock(doc=doc)

    res = CommentResolve().execute(ctx, name="missing")
    assert res == {
        "status": "error",
        "code": "COMMENT_NOT_FOUND",
        "message": "Comment 'missing' not found. Call comment_list to see the current names.",
    }


def test_comment_resolve_reply_insert_uses_anchor_host():
    from plugin.writer.specialized.comments import CommentResolve

    doc = MagicMock()
    _setup_mock_undo_manager(doc)
    body_text = MagicMock(name="body_text")
    doc.getText.return_value = body_text

    inserted = [False]
    host = MagicMock(name="cell_host")
    host.insertTextContent.side_effect = lambda *a, **k: inserted.__setitem__(0, True)

    field = _make_comment_field("target", host=host)
    reply_field = MagicMock(name="reply_field")
    reply_field.supportsService.return_value = True
    reply_field.getPropertyValue.return_value = "ReplyName"
    doc.createInstance.return_value = reply_field

    doc.getTextFields.return_value.createEnumeration.side_effect = (
        lambda: _FiniteEnum([field, reply_field]) if inserted[0] else _FiniteEnum([field])
    )

    ctx = MagicMock(doc=doc)
    with patch("plugin.writer.specialized.comments._set_annotation_date"):
        res = CommentResolve().execute(ctx, name="target", resolution="Fixed")
    assert res["status"] == "ok"
    assert res.get("reply_name") == "ReplyName"
    host.insertTextContent.assert_called_once()
    body_text.insertTextContent.assert_not_called()


def test_comment_resolve_assigns_non_empty_name():
    from plugin.writer.specialized.comments import CommentResolve

    doc = MagicMock()
    _setup_mock_undo_manager(doc)
    inserted = [False]
    host = MagicMock()
    host.insertTextContent.side_effect = lambda *a, **k: inserted.__setitem__(0, True)

    field = _make_comment_field("target", host=host)
    reply_field = MagicMock(name="reply_field")
    reply_field.supportsService.return_value = True
    assigned = {}

    def get_prop(p):
        if p == "Name":
            return assigned.get("Name", "")
        return ""

    def set_prop(p, v):
        assigned[p] = v

    reply_field.getPropertyValue.side_effect = get_prop
    reply_field.setPropertyValue.side_effect = set_prop
    doc.createInstance.return_value = reply_field

    doc.getTextFields.return_value.createEnumeration.side_effect = (
        lambda: _FiniteEnum([field, reply_field]) if inserted[0] else _FiniteEnum([field])
    )
    ctx = MagicMock(doc=doc)

    with patch("plugin.writer.specialized.comments._set_annotation_date"):
        res = CommentResolve().execute(ctx, name="target", resolution="Done")
    assert res["status"] == "ok"
    assert "Name" in assigned
    assert assigned["Name"].startswith("__Annotation__")
    assert res.get("reply_name") == assigned["Name"]


def test_add_comment_search_uses_find_occurrence_range():
    from plugin.writer.specialized.comments import AddComment

    doc = MagicMock()
    ctx = SimpleNamespace(doc=doc)
    match_range = MagicMock()
    match_range.getString.return_value = "found text"
    mtext = MagicMock()
    match_range.getText.return_value = mtext
    doc.getTextFields.return_value = _fields_that_grow()

    with patch("plugin.writer.search.find_occurrence_range", return_value=match_range) as mock_find, \
         patch("plugin.writer.search.find_all_ranges") as mock_find_all, \
         patch("plugin.writer.specialized.comments._set_annotation_date"):
        res = AddComment().execute(ctx, content="test note", search="find me", occurrence=1)

    assert res["status"] == "ok"
    mock_find.assert_called_once_with(doc, "find me", occurrence=1, case_sensitive_order=(True, False))
    mock_find_all.assert_not_called()


def test_add_comment_search_index_error_and_none_return_not_found():
    from plugin.writer.specialized.comments import AddComment

    doc = MagicMock()
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.writer.search.find_occurrence_range", side_effect=IndexError("out of range")):
        res_idx = AddComment().execute(ctx, content="note", search="hit", occurrence=3)
    assert res_idx == {
        "status": "error",
        "message": "Text 'hit' not found at occurrence 3.",
        "matched": False,
        "comment_added": False,
    }

    with patch("plugin.writer.search.find_occurrence_range", return_value=None):
        res_none = AddComment().execute(ctx, content="note", search="missing", occurrence=0)
    assert res_none == {
        "status": "error",
        "message": "Text 'missing' not found.",
        "matched": False,
        "comment_added": False,
    }


def test_comment_resolve_failed_reply_insert_does_not_set_resolved():
    from plugin.writer.specialized.comments import CommentResolve

    doc = MagicMock()
    _setup_mock_undo_manager(doc)
    field = _make_comment_field("target", host=MagicMock())
    doc.createInstance.return_value = MagicMock()
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum([field])
    ctx = MagicMock(doc=doc)

    with patch("plugin.writer.specialized.comments._set_annotation_date"):
        res = CommentResolve().execute(ctx, name="target", resolution="Failed to register")
    assert res["status"] == "error"
    assert "did not register an annotation" in res["message"]
    for call_item in field.setPropertyValue.call_args_list:
        assert call_item[0][0] != "Resolved"


def test_comments_none_author_and_content_do_not_raise():
    from plugin.writer.specialized.comments import CommentList, _comment_scan_tasks, _comment_check_stop

    field = MagicMock()
    field.supportsService.return_value = True
    field.getPropertyValue.side_effect = lambda p: {
        "Author": None,
        "Content": None,
        "Name": "c1",
        "ParentName": None,
        "Resolved": False,
        "DateTimeValue": SimpleNamespace(Year=2026, Month=1, Day=2, Hours=3, Minutes=4),
    }.get(p, None)
    anchor = MagicMock()
    anchor.getString.return_value = ""
    anchor.getText.return_value = MagicMock()
    field.getAnchor.return_value = anchor

    doc = MagicMock()
    doc.getText.return_value = MagicMock()
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum([field])
    doc_svc = MagicMock()
    doc_svc.get_paragraph_ranges.return_value = []
    ctx = MagicMock(doc=doc)
    ctx.services.document = doc_svc

    # 1. CommentList with author filter
    list_res = CommentList().execute(ctx, author_filter="test")
    assert list_res["status"] == "ok"
    assert list_res["comments"] == []

    # 2. _comment_scan_tasks
    scan_res = _comment_scan_tasks(ctx, {})
    assert scan_res["status"] == "ok"
    assert scan_res["count"] == 0

    # 3. _comment_check_stop
    stop_res = _comment_check_stop(ctx)
    assert stop_res["status"] == "ok"
    assert stop_res["should_stop"] is False


def test_comment_list_disposal_is_document_disposed():
    tool = CommentList()
    ctx = MagicMock()
    ctx.doc.getTextFields.side_effect = DocumentDisposedError("gone")
    res = tool.execute_safe(ctx)
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_comment_check_stop_whole_value():
    from plugin.writer.specialized.comments import _comment_check_stop

    def make_field(author: str, content: str, resolved: bool = False):
        f = MagicMock()
        f.supportsService.return_value = True
        f.getPropertyValue.side_effect = lambda prop: {
            "Content": content,
            "Resolved": resolved,
            "Author": author,
        }.get(prop, "")
        return f

    class FakeEnum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    # 1. Nonstop and Stop logging do not trip stop
    doc = MagicMock()
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: FakeEnum([
        make_field("MCP-WORKFLOW", "Status: nonstop\nTask: Stop logging"),
    ])
    ctx = MagicMock(doc=doc)
    res = _comment_check_stop(ctx)
    assert res["should_stop"] is False
    assert res["workflow_stop"] is False

    # 2. Status: pause trips stop
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: FakeEnum([
        make_field("MCP-WORKFLOW", "Status: pause"),
    ])
    res = _comment_check_stop(ctx)
    assert res["should_stop"] is True
    assert res["workflow_stop"] is True

    # 3. Bare stop line trips stop
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: FakeEnum([
        make_field("MCP-WORKFLOW", "step: 1\nstop"),
    ])
    res = _comment_check_stop(ctx)
    assert res["should_stop"] is True
    assert res["workflow_stop"] is True

    # 4. Unresolved STOP now comment sets stop_signals
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: FakeEnum([
        make_field("Reviewer", "STOP now please", resolved=False),
    ])
    res = _comment_check_stop(ctx)
    assert res["should_stop"] is True
    assert res["workflow_stop"] is False
    assert len(res["stop_signals"]) == 1
    assert res["stop_signals"][0]["author"] == "Reviewer"


def test_read_annotation_unconfirmed_zero_omits_paragraph_index():
    from plugin.writer.specialized.comments import _read_annotation

    anchor = MagicMock()
    anchor.getString.return_value = "hello"
    text_obj = MagicMock()
    anchor.getText.return_value = text_obj
    field = MagicMock()
    field.getAnchor.return_value = anchor
    field.getPropertyValue.side_effect = lambda p: {
        "Author": "User",
        "Content": "Comment",
        "DateTimeValue": SimpleNamespace(Year=2026, Month=1, Day=2, Hours=3, Minutes=4),
    }.get(p, "")
    doc_svc = MagicMock()
    doc_svc.find_paragraph_for_range.return_value = 0

    with patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.framework.uno_context.uno_same", return_value=True), \
         patch("plugin.doc.paragraph_search.confirm_paragraph_index", return_value=None):
        entry = _read_annotation(field, [], text_obj, doc_svc)

    assert "paragraph_index" not in entry


def test_comment_list_isolates_bad_comment():
    from plugin.writer.specialized.comments import CommentList

    field_good = MagicMock()
    field_good.supportsService.return_value = True
    field_good.getPropertyValue.side_effect = lambda p: {
        "Author": "Alice",
        "Content": "Looks good",
        "Name": "c1",
        "DateTimeValue": SimpleNamespace(Year=2026, Month=1, Day=2, Hours=3, Minutes=4),
    }.get(p, "")
    anchor_good = MagicMock()
    anchor_good.getString.return_value = "good"
    field_good.getAnchor.return_value = anchor_good

    field_bad = MagicMock()
    field_bad.supportsService.return_value = True
    field_bad.getPropertyValue.side_effect = RuntimeError("corrupted comment")

    class FiniteEnum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    doc = MagicMock()
    doc.getText.return_value = MagicMock()
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: FiniteEnum([field_bad, field_good])
    doc_svc = MagicMock()
    doc_svc.get_paragraph_ranges.return_value = []
    ctx = MagicMock(doc=doc)
    ctx.services.document = doc_svc

    res = CommentList().execute(ctx)
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert res["comments"][0]["stale"] is True
    assert "corrupted comment" in res["comments"][0]["error"]
    assert res["comments"][1]["author"] == "Alice"


def test_comment_list_propagates_real_disposal_from_item():
    from plugin.writer.specialized.comments import CommentList

    field_bad = MagicMock()
    field_bad.supportsService.return_value = True
    field_bad.getPropertyValue.side_effect = DocumentDisposedError("doc closed")

    class FiniteEnum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    doc = MagicMock()
    doc.getText.return_value = MagicMock()
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: FiniteEnum([field_bad])
    doc_svc = MagicMock()
    doc_svc.get_paragraph_ranges.return_value = []
    ctx = MagicMock(doc=doc)
    ctx.services.document = doc_svc

    res = CommentList().execute_safe(ctx)
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_comment_list_per_property_try_keeps_good_properties():
    from plugin.writer.specialized.comments import CommentList

    field = MagicMock()
    field.supportsService.return_value = True

    def get_prop(p):
        if p == "Content":
            raise RuntimeError("content failed")
        if p == "Author":
            return "Alice"
        if p == "DateTimeValue":
            return None
        return ""

    field.getPropertyValue.side_effect = get_prop
    doc = MagicMock()
    doc.getText.return_value = MagicMock()

    class OneEnum:
        def __init__(self):
            self.done = False
        def hasMoreElements(self):
            return not self.done
        def nextElement(self):
            self.done = True
            return field

    doc.getTextFields.return_value.createEnumeration.side_effect = OneEnum
    doc_svc = MagicMock()
    doc_svc.get_paragraph_ranges.return_value = []
    ctx = MagicMock(doc=doc)
    ctx.services.document = doc_svc

    res = CommentList().execute(ctx)
    assert res["status"] == "ok"
    assert len(res["comments"]) == 1
    assert res["comments"][0]["author"] == "Alice"
    assert "stale" not in res["comments"][0]


def test_read_annotation_disposal_propagates():
    from plugin.writer.specialized.comments import _read_annotation
    import pytest

    field = MagicMock()
    field.getPropertyValue.side_effect = DocumentDisposedError("doc closed")
    with pytest.raises(DocumentDisposedError):
        _read_annotation(field, [], MagicMock(), MagicMock())


def test_add_comment_empty_content_errors_without_search(monkeypatch):
    from plugin.writer.specialized.comments import AddComment

    searched = False

    def fake_find(*_args, **_kwargs):
        nonlocal searched
        searched = True
        return []

    monkeypatch.setattr("plugin.writer.search.find_all_ranges", fake_find)
    ctx = MagicMock()
    tool = AddComment()
    res = tool.execute(ctx, content="", search="hello")
    assert res["status"] == "error"
    assert "Provide content" in res["message"]
    assert not searched


def test_reply_and_resolve_call_shared_helper(monkeypatch):
    from plugin.writer.specialized import comments

    helper_calls = []

    def fake_insert_reply(doc, parent, parent_name, content, author):
        helper_calls.append((parent_name, content, author))
        if content == "fail":
            return False, "insert error"
        return True, "new_reply_name"

    monkeypatch.setattr(comments, "_insert_annotation_reply", fake_insert_reply)

    # 1. AddComment reply path
    target = MagicMock()
    monkeypatch.setattr(comments, "_find_annotation_by_name", lambda doc, name: target)
    ctx = MagicMock()
    _setup_mock_undo_manager(ctx.doc)
    add_res = comments.AddComment().execute(ctx, content="my reply", parent_name="c1", author="Author1")
    assert add_res["status"] == "ok"
    assert add_res["comment_added"] is True
    assert add_res["name"] == "new_reply_name"
    assert helper_calls[-1] == ("c1", "my reply", "Author1")

    # 2. CommentResolve failure does not set Resolved
    target.reset_mock()
    monkeypatch.setattr(comments, "_iter_annotations", lambda doc: [target])
    target.getPropertyValue.side_effect = lambda p: "c1" if p == "Name" else None
    resolve_fail = comments.CommentResolve().execute(ctx, name="c1", resolution="fail", author="AI")
    assert resolve_fail["status"] == "error"
    assert "comment_added" not in resolve_fail
    target.setPropertyValue.assert_not_called()

    # 3. CommentResolve success sets Resolved
    target.reset_mock()
    monkeypatch.setattr(comments, "_iter_annotations", lambda doc: [target])
    target.getPropertyValue.side_effect = lambda p: "c1" if p == "Name" else None
    resolve_ok = comments.CommentResolve().execute(ctx, name="c1", resolution="resolved it", author="AI")
    assert resolve_ok["status"] == "ok"
    assert resolve_ok["resolved"] is True
    assert resolve_ok["reply_name"] == "new_reply_name"
    assert "comment_added" not in resolve_ok
    target.setPropertyValue.assert_called_once_with("Resolved", True)


def test_comment_delete_subtree_and_author_isolation(monkeypatch):
    from plugin.writer.specialized import comments

    # Tree:
    # c1 (Alice, parent "")
    #  -> c2 (Alice, parent "c1")
    #      -> c3 (Bob, parent "c2")
    # c4 (Bob, parent "")
    # c5 (Alice, parent "")
    def make_field(name, parent, author):
        f = MagicMock()
        f.getPropertyValue.side_effect = lambda p: {"Name": name, "ParentName": parent, "Author": author}.get(p, "")
        anchor = MagicMock()
        text = MagicMock()
        anchor.getText.return_value = text
        f.getAnchor.return_value = anchor
        f._name = name
        return f

    f1 = make_field("c1", "", "Alice")
    f2 = make_field("c2", "c1", "Alice")
    f3 = make_field("c3", "c2", "Bob")
    f4 = make_field("c4", "", "Bob")
    f5 = make_field("c5", "", "Alice")

    all_fields = [f1, f2, f3, f4, f5]
    monkeypatch.setattr(comments, "_iter_annotations", lambda doc: list(all_fields))
    ctx = MagicMock()
    _setup_mock_undo_manager(ctx.doc)

    # Subtree delete of c1: removes c1, c2, c3
    res_sub = comments.CommentDelete().execute(ctx, name="c1")
    assert res_sub["status"] == "ok"
    assert res_sub["deleted"] == 3
    for f in (f1, f2, f3):
        f.getAnchor().getText().removeTextContent.assert_called_once_with(f)
    for f in (f4, f5):
        f.getAnchor().getText().removeTextContent.assert_not_called()

    # Reset
    for f in all_fields:
        f.getAnchor().getText().removeTextContent.reset_mock()

    # Author delete of Alice: removes c1, c2, c5 (does not delete Bob's reply c3 or Bob's c4)
    res_auth = comments.CommentDelete().execute(ctx, author="Alice")
    assert res_auth["status"] == "ok"
    assert res_auth["deleted"] == 3
    for f in (f1, f2, f5):
        f.getAnchor().getText().removeTextContent.assert_called_once_with(f)
    for f in (f3, f4):
        f.getAnchor().getText().removeTextContent.assert_not_called()


def test_comment_delete_second_removal_raises_rolls_back():
    from plugin.writer.specialized.comments import CommentDelete

    doc = MagicMock()
    mgr = _setup_mock_undo_manager(doc)
    host = MagicMock()
    field_a = _make_comment_field("A", "", "AuthorA", host=host)
    field_b = _make_comment_field("B", "A", "AuthorB", host=host)

    def remove_side_effect(field):
        if field == field_b:
            raise RuntimeError("remove failed")

    host.removeTextContent.side_effect = remove_side_effect

    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum([field_a, field_b])
    ctx = MagicMock(doc=doc)

    res = CommentDelete().execute(ctx, name="A")
    assert res["status"] == "error"
    assert res["deleted"] == 0
    assert "rolled back" in res["message"]
    mgr.undo.assert_called_once()


def test_comment_delete_locked_undo_manager():
    from plugin.writer.specialized.comments import CommentDelete

    doc = MagicMock()
    mgr = _setup_mock_undo_manager(doc)
    mgr.isLocked.return_value = True
    host = MagicMock()
    field_a = _make_comment_field("A", "", "AuthorA", host=host)
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum([field_a])
    ctx = MagicMock(doc=doc)

    res = CommentDelete().execute(ctx, name="A")
    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"
    host.removeTextContent.assert_not_called()


def test_comment_resolve_locked_undo_manager():
    from plugin.writer.specialized.comments import CommentResolve

    doc = MagicMock()
    mgr = _setup_mock_undo_manager(doc)
    mgr.isLocked.return_value = True
    field = _make_comment_field("target")
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum([field])
    ctx = MagicMock(doc=doc)

    res = CommentResolve().execute(ctx, name="target")
    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"
    field.setPropertyValue.assert_not_called()


def test_comment_list_author_filter_skips_location_helper_on_mismatch():
    from plugin.writer.specialized.comments import CommentList

    doc = MagicMock()
    field_match = MagicMock()
    field_match.supportsService.return_value = True
    field_match.getPropertyValue.side_effect = lambda p: "Alice" if p == "Author" else ""

    field_mismatch = MagicMock()
    field_mismatch.supportsService.return_value = True
    field_mismatch.getPropertyValue.side_effect = lambda p: "Bob" if p == "Author" else ""

    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum([field_mismatch, field_match])

    doc_svc = MagicMock()
    doc_svc.get_paragraph_ranges.return_value = []
    ctx = MagicMock(doc=doc)
    ctx.services.document = doc_svc

    with patch("plugin.writer.specialized.comments._read_annotation", return_value={"author": "Alice"}) as mock_read:
        res = CommentList().execute(ctx, author_filter="Alice")

    assert res["count"] == 1
    assert mock_read.call_count == 1
    assert mock_read.call_args[0][0] == field_match


def test_comment_scan_tasks_reraises_disposal():
    import pytest
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.specialized.comments import _comment_scan_tasks

    doc = MagicMock()
    field = MagicMock()
    field.supportsService.return_value = True
    field.getPropertyValue.side_effect = DocumentDisposedError("disposed on Content")
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum([field])

    doc_svc = MagicMock()
    doc_svc.get_paragraph_ranges.return_value = []
    ctx = MagicMock(doc=doc)
    ctx.services.document = doc_svc

    with pytest.raises(DocumentDisposedError):
        _comment_scan_tasks(ctx, {})

    # Disposed on Resolved property
    field2 = MagicMock()
    field2.supportsService.return_value = True

    def get_prop(p):
        if p == "Content":
            return "TODO-AI: fix this"
        if p == "Resolved":
            raise DocumentDisposedError("disposed on Resolved")
        return ""

    field2.getPropertyValue.side_effect = get_prop
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum([field2])

    with pytest.raises(DocumentDisposedError):
        _comment_scan_tasks(ctx, {"unresolved_only": True})

