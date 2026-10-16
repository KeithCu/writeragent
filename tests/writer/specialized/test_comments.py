# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Comment helper tests (list/read paths). No LibreOffice required."""
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch


def test_read_annotation_falls_back_to_paragraph_context():
    from plugin.writer.specialized.comments import _read_annotation

    anchor = MagicMock()
    anchor.getString.return_value = ""
    field = MagicMock()
    field.getAnchor.return_value = anchor
    field.getPropertyValue.side_effect = lambda p: {"Author": "Rev", "Content": "note"}.get(p, "")
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
    assert not getattr(comments.CommentScanTasks, "is_mutation", False)
    assert not hasattr(comments, "CommentWorkflow")


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


def test_add_comment_reply_requires_content():
    from plugin.writer.specialized.comments import AddComment

    ctx = MagicMock()
    res = AddComment().execute(ctx, parent_name="some-parent")
    assert res["status"] == "error"
    assert "content" in res["message"].lower()
    ctx.doc.getTextFields.assert_not_called()


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


def test_delete_comment_by_name_removes_thread_chain():
    from plugin.writer.specialized.comments import CommentDelete

    doc = MagicMock()
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
    body_text = MagicMock(name="body_text")
    doc.getText.return_value = body_text

    host = MagicMock(name="cell_host")
    field = _make_comment_field("target", host=host)
    doc.getTextFields.return_value.createEnumeration.side_effect = lambda: _FiniteEnum([field])
    reply_field = MagicMock(name="reply_field")
    doc.createInstance.return_value = reply_field

    ctx = MagicMock(doc=doc)
    with patch("plugin.writer.specialized.comments._set_annotation_date"):
        res = CommentResolve().execute(ctx, name="target", resolution="Fixed")
    assert res["status"] == "ok"
    host.insertTextContent.assert_called_once()
    body_text.insertTextContent.assert_not_called()
