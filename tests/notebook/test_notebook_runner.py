# WriterAgent - tests for notebook cell execution

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.notebook.cell_registry import (
    NotebookDocState,
    cell_id_to_hex,
    new_code_cell_entry,
)
from plugin.notebook import form_lookup
from plugin.notebook.notebook_runner import (
    _gutter_text_cursor,
    _is_next_cell_boundary,
    _paragraph_has_frame,
    _paragraph_string,
    _execute_and_apply,
    apply_run_result,
    clear_cell_output,
    execute_code,
    find_run_from_here_index,
    format_run_output_text,
    request_stop,
    run_cell,
    run_cell_for_doc_hex,
    run_cells,
    update_in_prompt,
)


def test_format_run_output_text_stdout_and_result():
    text = format_run_output_text({"status": "ok", "stdout": "hi\n", "result": 42}, execution_count=1)
    assert "hi" in text
    assert "42" in text
    assert "Out [1]:" in text
    assert text.index("hi") < text.index("Out [1]:")


def test_format_run_output_text_stdout_has_no_out_prompt():
    text = format_run_output_text({"status": "ok", "stdout": "printed\n", "result": None})
    assert text == "printed"
    assert "Out [" not in text


def test_format_run_output_text_error_traceback():
    text = format_run_output_text(
        {"status": "error", "traceback": "\x1b[31mValueError\x1b[0m: bad", "stdout": ""}
    )
    assert "ValueError" in text
    assert "\x1b" not in text


def test_format_run_output_text_strips_stream_ansi_and_cr():
    result = {"status": "ok", "stdout": "\x1b[32mgo\x1b[0m\rdone\n"}
    formatted = format_run_output_text(result)
    assert "done" in formatted
    assert "\x1b" not in formatted
    assert "go" not in formatted


def test_format_run_output_text_skips_image_result():
    wire = {"__wa_payload__": "image", "data": b"x", "format": "png"}
    with patch("plugin.notebook.notebook_runner.is_image_payload", return_value=True):
        text = format_run_output_text({"status": "ok", "result": wire, "stdout": ""})
    assert text == ""


def test_format_run_output_text_hides_multi_data_of_images():
    from plugin.scripting.payload_codec import pack_image_envelope, pack_multi_data_envelope

    img = pack_image_envelope(b"png-bytes", "png")
    only_images = pack_multi_data_envelope([img, img])
    assert format_run_output_text({"status": "ok", "stdout": "", "result": only_images}) == ""

    mixed = pack_multi_data_envelope([img, 1])
    assert format_run_output_text({"status": "ok", "stdout": "", "result": mixed}) != ""


def test_format_run_output_text_unpack_failure_renders_raw_wire():
    wire = {"raw": "payload"}
    with patch("plugin.notebook.notebook_runner.host_unpack_data", side_effect=ValueError("bad")):
        text = format_run_output_text({"status": "ok", "result": wire}, execution_count=2)
    assert repr(wire) in text


def _enum_of(items):
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True] * len(items) + [False]
    enum.nextElement.side_effect = items
    return enum


def test_execute_code_does_not_pump_idle():
    ctx = MagicMock()
    doc = MagicMock()
    worker_result = {"status": "ok", "result": 1, "stdout": ""}

    with (
        patch("plugin.notebook.notebook_runner.notebook_session_id", return_value="notebook:test"),
        patch("plugin.notebook.notebook_runner.run_blocking_in_thread", return_value=worker_result) as pump,
        patch("plugin.notebook.notebook_runner.run_code_in_user_venv") as run_venv,
        patch("plugin.notebook.notebook_runner.pin_script_document", return_value="doc:test"),
        patch("plugin.notebook.notebook_runner.release_script_document") as release,
    ):
        out = execute_code(ctx, doc, "x = 1")
        assert out == worker_result
        pump.assert_called_once()
        assert pump.call_args.kwargs.get("pump_idle") is False
        run_venv.assert_not_called()

        # Call the mapped function to test inner logic
        func = pump.call_args.args[1]
        func()
        run_venv.assert_called_once()
        kwargs = run_venv.call_args.kwargs
        assert kwargs.get("python_tool_domain") is None
        assert kwargs.get("session_id") == "notebook:test"
        assert kwargs.get("script_session_id") == "doc:test"
        from plugin.framework.send_cancellation import get_current_send_cancellation

        assert kwargs.get("cancel") is get_current_send_cancellation()
        release.assert_called_once_with("doc:test")


def test_execute_code_latched_stop_skips_venv():
    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.notebook.notebook_runner.notebook_session_id", return_value="notebook:test"),
        patch(
            "plugin.notebook.notebook_runner.run_blocking_in_thread",
            side_effect=lambda ctx, func, **kw: func(),
        ),
        patch("plugin.notebook.notebook_runner.run_code_in_user_venv") as run_venv,
        patch("plugin.notebook.notebook_runner._is_stop_requested", return_value=True),
        patch("plugin.notebook.notebook_runner.pin_script_document", return_value="doc:test"),
        patch("plugin.notebook.notebook_runner.release_script_document") as release,
    ):
        out = execute_code(ctx, doc, "x = 1")
    assert out["status"] == "stopped"
    run_venv.assert_not_called()
    release.assert_called_once_with("doc:test")


def test_execute_code_passes_current_send_scope():
    from plugin.framework.send_cancellation import SendCancellation

    scope = SendCancellation()
    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.notebook.notebook_runner.notebook_session_id", return_value="notebook:test"),
        patch(
            "plugin.notebook.notebook_runner.run_blocking_in_thread",
            side_effect=lambda ctx, func, **kw: func(),
        ),
        patch("plugin.notebook.notebook_runner.run_code_in_user_venv", return_value={"status": "ok"}) as run_venv,
        patch("plugin.notebook.notebook_runner._is_stop_requested", return_value=False),
        patch("plugin.notebook.notebook_runner.pin_script_document", return_value="doc:test"),
        patch("plugin.notebook.notebook_runner.release_script_document"),
        patch("plugin.framework.send_cancellation.get_current_send_cancellation", return_value=scope),
    ):
        execute_code(ctx, doc, "x = 1")
    assert run_venv.call_args.kwargs["cancel"] is scope


def test_run_cell_updates_registry_and_execution_count():
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=5)
    doc = MagicMock()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value="print(1)"),
        patch(
            "plugin.notebook.notebook_runner.execute_code",
            return_value={"status": "ok", "result": None, "stdout": "1\n"},
        ),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry") as save_reg,
    ):
        result = run_cell(ctx, doc, cell.cell_id)

    assert result.status == "ok"
    assert result.execution_count == 5
    assert cell.execution_count == 5
    assert state.next_execution_count == 6
    save_reg.assert_called_once_with(doc, state)


def test_run_cell_restores_view_to_cell():
    """Restore runs after output insertion, so the view lands on the run cell, not the document end where insertion left it."""
    ctx = MagicMock()
    doc = MagicMock()
    vc = doc.getCurrentController.return_value.getViewCursor.return_value
    anchor = MagicMock()
    shape = MagicMock()
    shape.getAnchor.return_value = anchor
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=1)
    events: list[tuple[object, ...]] = []
    vc.gotoRange.side_effect = lambda *args: events.append(("goto", args))

    with (
        patch("plugin.notebook.notebook_runner.execute_code", return_value={"status": "ok", "stdout": "", "result": None}),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result", side_effect=lambda *_a, **_k: events.append(("apply",))),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.form_lookup.find_control_shape_by_name", return_value=shape),
    ):
        _execute_and_apply(ctx, doc, state, cell, "x = 1")

    assert events == [("apply",), ("goto", (anchor, False))]


def test_run_cell_rejects_reentrant_call():
    """A nested run_cell (second ▶ while the first is in execute_code) must not
    increment the execution counter or clobber output."""
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=5)
    doc = MagicMock()
    nested: list[object] = []

    def _reenter(*_a: object, **_k: object) -> dict[str, object]:
        nested.append(run_cell(ctx, doc, cell.cell_id))
        return {"status": "ok", "result": None, "stdout": "1\n"}

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value="print(1)"),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_reenter),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry") as save_reg,
    ):
        result = run_cell(ctx, doc, cell.cell_id)

    assert result.status == "ok"
    assert result.execution_count == 5
    assert len(nested) == 1
    assert getattr(nested[0], "status") == "busy"
    assert state.next_execution_count == 6
    save_reg.assert_called_once_with(doc, state)


def test_run_cell_empty_code():
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell])
    doc = MagicMock()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value="   "),
    ):
        result = run_cell(ctx, doc, cell.cell_id)
    assert result.status == "error"
    assert "empty" in result.message.lower()


def test_run_cell_missing_code_field():
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell])
    doc = MagicMock()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value=None),
    ):
        result = run_cell(ctx, doc, cell.cell_id)
    assert result.status == "error"
    assert "missing" in result.message.lower()
    assert "empty" not in result.message.lower()


def test_run_cell_busy_guard_shares_uid_key_across_wrappers():
    """Two PyUNO wrappers of one doc must not both run."""
    from plugin.notebook.layout import doc_key

    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=1)
    doc1 = MagicMock()
    doc2 = MagicMock()
    for doc in (doc1, doc2):
        doc.getRuntimeUID.return_value = "uid-same"
    assert doc_key(doc1) == doc_key(doc2) == "uid:uid-same"
    nested: list[object] = []

    def _reenter(*_a: object, **_k: object) -> dict[str, object]:
        nested.append(run_cell(ctx, doc2, cell.cell_id))
        return {"status": "ok", "result": None, "stdout": "1\n"}

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value="print(1)"),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_reenter),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
    ):
        result = run_cell(ctx, doc1, cell.cell_id)

    assert result.status == "ok"
    assert len(nested) == 1
    assert getattr(nested[0], "status") == "busy"


def test_shared_notebook_session_via_sandbox(fresh_sandbox_sessions):
    from tests.scripting.sandbox_support import sandbox_run

    sid = "notebook:test-runner"
    r1 = sandbox_run("x = 41\nresult = x + 1", None, session_id=sid)
    assert r1["status"] == "ok"
    r2 = sandbox_run("result = x + 1", None, session_id=sid)
    assert r2["status"] == "ok"
    assert r2["result"] == 42


def test_is_next_cell_boundary_markdown_and_code():
    assert _is_next_cell_boundary("Heading 3", "Cell 3: Markdown", None) is True
    assert _is_next_cell_boundary("Heading 3", "Cell 5: Raw", None) is True
    assert _is_next_cell_boundary("Preformatted Text", "old stdout", None) is False
    assert _is_next_cell_boundary("WriterAgent Notebook In", "In [1]:", "WriterAgent Notebook In") is True
    assert _is_next_cell_boundary("WriterAgent Notebook In", "", "WriterAgent Notebook In") is False
    assert _is_next_cell_boundary("Heading 2", "1. Creating Arrays", None) is True
    assert _is_next_cell_boundary("Text Body", "A transpose swaps axes.", None) is True
    assert _is_next_cell_boundary("Preformatted Text", "Out [1]: 42", None) is False
    # Fallback stdout style must not count as the next cell, or re-runs stack.
    assert _is_next_cell_boundary("WriterAgent Notebook Output", "old stdout", None) is False
    assert _is_next_cell_boundary("Quotations", "A blockquote", None) is True
    assert _is_next_cell_boundary("List 1 Start", "A list item", None) is True


def test_paragraph_string_uses_selection_when_nonempty():
    cursor = MagicMock()
    cursor.getString.return_value = "Cell 3: Markdown"
    assert _paragraph_string(cursor) == "Cell 3: Markdown"
    cursor.getText.assert_not_called()


def test_paragraph_string_expands_collapsed_cursor():
    # Live Writer: collapsed XTextCursor.getString() is "" (selection, not paragraph).
    cursor = MagicMock()
    cursor.getString.return_value = ""
    probe = MagicMock()
    probe.getString.return_value = "Cell 3: Markdown"
    cursor.getText.return_value.createTextCursorByRange.return_value = probe
    assert _paragraph_string(cursor) == "Cell 3: Markdown"
    probe.gotoStartOfParagraph.assert_called_once_with(False)
    probe.gotoEndOfParagraph.assert_called_once_with(True)


class _FakePos:
    def __init__(self, index: int) -> None:
        self.index = index


class _FakeCursor:
    def __init__(self, paras: list[tuple[str, str]], text: "_FakeXText", index: int = 0, collapsed: bool = False) -> None:
        self.paras = paras
        self._text = text
        self.index = index
        self._end_index = index
        self._collapsed = collapsed
        self._expanded = False

    @property
    def ParaStyleName(self) -> str:
        return self.paras[self.index][0]

    def getText(self) -> "_FakeXText":
        return self._text

    def getString(self) -> str:
        if self._collapsed and not self._expanded:
            return ""
        return self.paras[self.index][1]

    def getStart(self) -> _FakePos:
        return _FakePos(self.index)

    def gotoStartOfParagraph(self, _expand: bool) -> None:
        return None

    def gotoEndOfParagraph(self, expand: bool) -> None:
        self._expanded = expand

    def gotoNextParagraph(self, _expand: bool) -> bool:
        if self.index + 1 >= len(self.paras):
            return False
        self.index += 1
        return True

    def gotoEnd(self, _expand: bool) -> None:
        self.index = len(self.paras)

    def gotoRange(self, pos: _FakePos, expand: bool) -> None:
        if expand:
            self._end_index = pos.index
        else:
            self.index = pos.index

    def setString(self, value: str) -> None:
        assert value == ""
        del self.paras[self.index : self._end_index]


class _FakeXText:
    def __init__(self, paras: list[tuple[str, str]], collapsed: bool = False) -> None:
        self.paras = paras
        self.collapsed = collapsed

    def createTextCursorByRange(self, pos: Any) -> _FakeCursor:
        return _FakeCursor(self.paras, self, index=pos.index, collapsed=self.collapsed)

    def deleteContents(self, *_args: Any) -> None:
        raise AssertionError("deleteContents must not be called")


def _walker_scenario(paragraphs: list[tuple[str, str]], *, collapsed: bool = False) -> list[tuple[str, str]]:
    paras = list(paragraphs)
    text = _FakeXText(paras, collapsed=collapsed)
    start = _FakeCursor(paras, text, index=0, collapsed=collapsed)
    doc = MagicMock()
    doc.getText.return_value = text
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    with (
        patch("plugin.notebook.notebook_runner._cursor_after_bookmark", return_value=start),
        patch("plugin.notebook.notebook_runner._code_field_paragraph_end", return_value=None),
        patch("plugin.notebook.notebook_runner._is_output_bookmark_home", return_value=False),
        patch("plugin.notebook.notebook_runner._paragraph_has_frame", return_value=False),
        patch("plugin.notebook.notebook_runner.resolve_para_style", return_value="WriterAgent Notebook In"),
    ):
        clear_cell_output(doc, cell)
    return paras


@pytest.mark.parametrize(
    "paragraphs,expected_remaining,collapsed",
    [
        pytest.param(
            [("Preformatted Text", "old stdout"), ("WriterAgent Notebook In", "[In [ ]]\tCell 2: Code")],
            [("WriterAgent Notebook In", "[In [ ]]\tCell 2: Code")],
            False,
            id="uses_set_string_not_delete_contents",
        ),
        pytest.param(
            [("Preformatted Text", "Array: [10 20 30]"), ("Quotations", "A blockquote")],
            [("Quotations", "A blockquote")],
            False,
            id="stops_at_markdown_blockquote",
        ),
        pytest.param(
            [("Preformatted Text", "Array: [10 20 30]"), ("Heading 3", "Cell 3: Markdown")],
            [("Heading 3", "Cell 3: Markdown")],
            False,
            id="stops_at_markdown_cell_heading",
        ),
        pytest.param(
            [("Preformatted Text", "old stdout"), ("Heading 3", "Cell 3: Markdown")],
            [("Heading 3", "Cell 3: Markdown")],
            True,
            id="collapsed_cursor_stops_at_markdown",
        ),
        pytest.param(
            [("Text Body", ""), ("Preformatted Text", "old stdout"), ("Heading 2", "Reshape and transpose")],
            [("Text Body", ""), ("Heading 2", "Reshape and transpose")],
            False,
            id="skips_control_row_then_clears_stdout",
        ),
    ],
)
def test_clear_cell_output_stops_at_boundaries(paragraphs, expected_remaining, collapsed):
    assert _walker_scenario(paragraphs, collapsed=collapsed) == expected_remaining


def test_clear_cell_output_skips_empty_control_paragraph():
    """▶+field getString is empty (frames omitted). setString there deleted ▶ and nb_out_*."""
    paras = [("Text Body", ""), ("Heading 2", "Reshape and transpose")]
    assert _walker_scenario(paras) == paras


def test_is_output_bookmark_home_empty_text_body_not_heading():
    from plugin.notebook.notebook_runner import _is_output_bookmark_home

    doc = MagicMock()
    cell = new_code_cell_entry(2, 2, "nb_cell_2_code")

    control = MagicMock()
    control.ParaStyleName = "Text Body"
    control.getString.return_value = ""

    from plugin.notebook.notebook_runner import _is_leftover_empty_paragraph

    assert _is_leftover_empty_paragraph(control) is True

    heading = MagicMock()
    heading.ParaStyleName = "Heading 2"
    heading.getString.return_value = ""

    stdout = MagicMock()
    stdout.ParaStyleName = "Preformatted Text"
    stdout.getString.return_value = ""

    prompt = MagicMock()
    prompt.ParaStyleName = "WriterAgent Notebook In"
    prompt.getString.return_value = "In [1]:"

    with patch("plugin.notebook.notebook_runner._code_field_paragraph_end", return_value=None):
        assert _is_output_bookmark_home(control, doc, cell) is False
        assert _is_output_bookmark_home(heading, doc, cell) is False
        assert _is_output_bookmark_home(stdout, doc, cell) is False
        assert _is_output_bookmark_home(prompt, doc, cell) is True

    nxt = MagicMock()
    nxt.ParaStyleName = "WriterAgent Notebook In"
    nxt.getString.return_value = "In [3]:"
    with (
        patch("plugin.notebook.notebook_runner._code_field_paragraph_end", return_value=MagicMock()),
        patch("plugin.notebook.notebook_runner._same_paragraph", return_value=False),
    ):
        assert _is_output_bookmark_home(nxt, MagicMock(), cell) is False


def test_run_cell_rerun_clears_then_applies():
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=1)
    doc = MagicMock()
    order: list[str] = []

    def rec_clear(*_a, **_k):
        order.append("clear")

    def rec_apply(*_a, **_k):
        order.append("apply")

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value="print(1)"),
        patch(
            "plugin.notebook.notebook_runner.execute_code",
            return_value={"status": "ok", "result": None, "stdout": "1\n"},
        ),
        patch("plugin.notebook.notebook_runner.clear_cell_output", side_effect=rec_clear),
        patch("plugin.notebook.notebook_runner.apply_run_result", side_effect=rec_apply),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
    ):
        assert run_cell(ctx, doc, cell.cell_id).status == "ok"
        assert run_cell(ctx, doc, cell.cell_id).status == "ok"

    assert order == ["clear", "apply", "clear", "apply"]


def test_apply_run_result_replaces_via_clear_then_insert():
    """Re-run path: clear empties the range; apply writes the new stdout under Output."""
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    cursor = MagicMock()
    cursor.getString.return_value = ""
    cursor.ParaStyleName = "Preformatted Text"
    cursor.goRight.return_value = True
    text = MagicMock()
    doc = MagicMock()
    doc.getText.return_value = text

    with (
        patch("plugin.notebook.notebook_runner._cursor_after_bookmark", return_value=cursor),
        patch("plugin.notebook.notebook_runner.resolve_para_style", side_effect=lambda _doc, name: name),
    ):
        apply_run_result(doc, cell, {"status": "ok", "stdout": "first\n", "result": None})
        apply_run_result(doc, cell, {"status": "ok", "stdout": "second\n", "result": None})

    assert [call.args[1] for call in text.insertString.call_args_list] == ["first", "second"]
    text.deleteContents.assert_not_called()
    cursor.gotoEnd.assert_not_called()
    text.insertControlCharacter.assert_not_called()


def test_insert_stdout_paragraph_splits_before_next_cell_chrome():
    """When the cursor lands on Cell N: Markdown, stdout must not replace that heading."""
    from plugin.notebook.layout import PARAGRAPH_BREAK
    from plugin.notebook.notebook_runner import _insert_stdout_paragraph

    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    cursor = MagicMock()
    cursor.ParaStyleName = "Heading 3"
    cursor.goRight.return_value = True
    cursor.getString.return_value = "Cell 3: Markdown"
    text = MagicMock()
    nxt = MagicMock()
    nxt.gotoNextParagraph.return_value = False
    nxt.getString.return_value = "Cell 3: Markdown"
    nxt.ParaStyleName = "Heading 3"
    text.createTextCursorByRange.return_value = nxt
    doc = MagicMock()
    doc.getText.return_value = text

    with patch("plugin.notebook.notebook_runner._cursor_after_bookmark", return_value=cursor):
        _insert_stdout_paragraph(doc, cell, cursor, "hello", "Preformatted Text", "WriterAgent Notebook In")

    assert [call.args[1] for call in text.insertControlCharacter.call_args_list] == [
        PARAGRAPH_BREAK,
        PARAGRAPH_BREAK,
    ]
    text.insertString.assert_called_once_with(cursor, "hello", False)
    cursor.setString.assert_not_called()
    cursor.goRight.assert_called()


def test_insert_stdout_paragraph_does_not_split_long_stdout_in_empty_paragraph():
    from plugin.notebook.notebook_runner import _insert_stdout_paragraph

    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    cursor = MagicMock()
    cursor.getString.return_value = ""
    cursor.ParaStyleName = "Preformatted Text"
    text = MagicMock()
    doc = MagicMock()
    doc.getText.return_value = text

    with (
        patch("plugin.notebook.notebook_runner._split_if_stdout_mashed_onto_chrome") as split,
        patch("plugin.notebook.notebook_runner._reanchor_output_bookmark"),
        patch("plugin.notebook.notebook_runner._collapse_leading_empty_paragraphs"),
    ):
        _insert_stdout_paragraph(doc, cell, cursor, "x" * 40_000, "Preformatted Text", "WriterAgent Notebook In")

    split.assert_not_called()
    text.insertString.assert_called_once()


def test_gutter_text_cursor_stops_before_frame_portion():
    """setString must not cover AS_CHARACTER ControlShapes (▶ / code field)."""
    text_portion = MagicMock(name="text_portion")
    text_portion.getPropertyValue.return_value = "Text"
    frame_portion = MagicMock(name="frame_portion")
    frame_portion.getPropertyValue.return_value = "Frame"

    paragraph = MagicMock(name="paragraph")
    paragraph.createEnumeration.return_value = _enum_of([text_portion, frame_portion])

    # para is a text cursor. Its enumeration yields the paragraph, not portions.
    para = MagicMock()
    para.createEnumeration.return_value = _enum_of([paragraph])

    text_cursor = MagicMock(name="text_cursor")
    text = MagicMock()
    text.createTextCursorByRange.return_value = text_cursor

    cursor = _gutter_text_cursor(text, para)
    assert cursor is text_cursor
    text.createTextCursorByRange.assert_called_once_with(text_portion.getStart.return_value)
    text_cursor.gotoRange.assert_called_once_with(text_portion.getEnd.return_value, True)
    assert all(call.args[0] is not frame_portion for call in text.createTextCursorByRange.call_args_list)
    paragraph.getPropertyValue.assert_not_called()


def test_update_in_prompt_does_not_setstring_whole_paragraph():
    """Whole-para setString deletes in-flow ▶ on the In [n]: gutter. Rewrite Text only."""
    text_portion = MagicMock(name="text_portion")
    text_portion.getPropertyValue.return_value = "Text"
    frame_portion = MagicMock(name="frame_portion")
    frame_portion.getPropertyValue.return_value = "Frame"

    paragraph = MagicMock(name="paragraph")
    paragraph.createEnumeration.return_value = _enum_of([text_portion, frame_portion])

    text_cursor = MagicMock(name="text_cursor")
    nav = MagicMock(name="nav")
    nav.gotoPreviousParagraph.return_value = True
    gutter = MagicMock(name="gutter")
    gutter.createEnumeration.return_value = _enum_of([paragraph])

    text = MagicMock()

    def _cursor_for(rng):
        if rng == "anchor":
            return nav
        if rng is nav:
            return gutter
        if rng is text_portion.getStart.return_value:
            return text_cursor
        raise AssertionError(rng)

    text.createTextCursorByRange.side_effect = _cursor_for
    doc = MagicMock()
    doc.getText.return_value = text
    shape = MagicMock()
    shape.getAnchor.return_value = "anchor"

    cell = new_code_cell_entry(1, 1, "nb_cell_1_code")
    with patch("plugin.notebook.form_lookup.find_control_shape_by_name", return_value=shape):
        update_in_prompt(doc, cell, 4)

    text_cursor.setString.assert_called_once_with("In [4]:")
    gutter.setString.assert_not_called()
    text.createEnumeration.assert_not_called()


def test_paragraph_has_frame_reads_portion_not_paragraph():
    """Cursor enumeration yields a paragraph; Frame is on that paragraph's portions."""
    frame = MagicMock(name="frame")
    frame.getPropertyValue.return_value = "Frame"
    paragraph = MagicMock(name="paragraph")
    paragraph.createEnumeration.return_value = _enum_of([frame])

    para_rng = MagicMock(name="para_rng")
    para_rng.createEnumeration.return_value = _enum_of([paragraph])
    text = MagicMock()
    text.createTextCursorByRange.return_value = para_rng
    cursor = MagicMock()
    cursor.getText.return_value = text

    assert _paragraph_has_frame(cursor) is True
    frame.getPropertyValue.assert_called_with("TextPortionType")
    paragraph.getPropertyValue.assert_not_called()

    text_only = MagicMock(name="text_portion")
    text_only.getPropertyValue.return_value = "Text"
    paragraph.createEnumeration.return_value = _enum_of([text_only])
    para_rng.createEnumeration.return_value = _enum_of([paragraph])
    assert _paragraph_has_frame(cursor) is False


def test_update_in_prompt_does_not_rewrite_first_gutter_when_shape_missing():
    """A missing ControlShape must not setString the first ``In [n]:`` in the doc."""
    first = MagicMock(name="first_gutter")
    first.getString.return_value = "In [1]:"
    second = MagicMock(name="second_gutter")
    second.getString.return_value = "In [2]:"
    text = MagicMock()
    text.createEnumeration.return_value = _enum_of([first, second])
    doc = MagicMock()
    doc.getText.return_value = text
    cell = new_code_cell_entry(1, 1, "nb_cell_1_code")

    with patch("plugin.notebook.form_lookup.find_control_shape_by_name", return_value=None):
        update_in_prompt(doc, cell, 9)

    text.createEnumeration.assert_not_called()
    text.createTextCursorByRange.assert_not_called()
    first.setString.assert_not_called()
    second.setString.assert_not_called()


def test_run_cell_error_still_increments_execution_count():
    """Failed runs consume a kernel count, matching Jupyter In [n]."""
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=1)
    doc = MagicMock()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value="raise ValueError(1)"),
        patch(
            "plugin.notebook.notebook_runner.execute_code",
            return_value={"status": "error", "message": "ValueError", "stdout": ""},
        ),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
    ):
        result = run_cell(ctx, doc, cell.cell_id)

    assert result.status == "error"
    assert result.execution_count == 1
    assert cell.execution_count == 1
    assert state.next_execution_count == 2


def test_insert_stdout_paragraph_no_break_when_para_empty():
    from plugin.notebook.notebook_runner import _insert_stdout_paragraph

    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    cursor = MagicMock()
    cursor.getString.return_value = ""
    cursor.ParaStyleName = "Preformatted Text"
    text = MagicMock()
    nxt = MagicMock()
    nxt.gotoNextParagraph.return_value = False
    text.createTextCursorByRange.return_value = nxt
    doc = MagicMock()
    doc.getText.return_value = text

    with patch("plugin.notebook.notebook_runner._cursor_after_bookmark", return_value=cursor):
        _insert_stdout_paragraph(
            doc, cell, cursor, "NumPy Version: 2.5.2", "Preformatted Text", "WriterAgent Notebook In"
        )

    text.insertControlCharacter.assert_not_called()
    text.insertString.assert_called_once_with(cursor, "NumPy Version: 2.5.2", False)


def test_apply_run_result_missing_bookmark_does_not_append_at_end():
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    doc = MagicMock()
    text = MagicMock()
    doc.getText.return_value = text
    with (
        patch("plugin.notebook.notebook_runner._reanchor_output_bookmark", return_value=None),
        patch("plugin.notebook.notebook_runner._cell_end_range", return_value=None),
        patch("plugin.notebook.notebook_runner._insert_stdout_paragraph") as insert,
    ):
        apply_run_result(doc, cell, {"status": "ok", "stdout": "dumped\n", "result": None})
    insert.assert_not_called()
    text.insertString.assert_not_called()
    text.insertControlCharacter.assert_not_called()


def test_run_cell_for_doc_hex_execution_error_does_not_msgbox():
    """InterpreterError / traceback is inline; do not modal after apply_run_result."""
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=1)
    doc = MagicMock()
    ctx = MagicMock()
    hex_id = cell_id_to_hex(cell.cell_id)
    apply_calls: list[object] = []

    def rec_apply(_doc, _cell, result, *, ctx=None):
        apply_calls.append(result)

    with (
        patch("plugin.notebook.notebook_runner.is_writer", return_value=True),
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value="car[:,:,:3].shape"),
        patch(
            "plugin.notebook.notebook_runner.execute_code",
            return_value={
                "status": "error",
                "message": "InterpreterError: name 'car' is not defined",
                "stdout": "",
                "traceback": "InterpreterError: name 'car' is not defined",
            },
        ),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result", side_effect=rec_apply),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.msgbox") as boxed,
    ):
        run_cell_for_doc_hex(ctx, doc, hex_id)

    boxed.assert_not_called()
    assert apply_calls
    assert isinstance(apply_calls, list) and len(apply_calls) > 0 and isinstance(apply_calls[0], dict) and apply_calls[0].get("status") == "error"

def test_run_cell_for_doc_hex_setup_error_shows_msgbox():
    """Empty code or missing field surfaces a msgbox, as there's no result inline."""
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=1)
    doc = MagicMock()
    ctx = MagicMock()
    hex_id = cell_id_to_hex(cell.cell_id)

    with (
        patch("plugin.notebook.notebook_runner.is_writer", return_value=True),
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value="   "),  # Empty code
        patch("plugin.notebook.notebook_runner.execute_code") as execute,
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.msgbox") as boxed,
    ):
            run_cell_for_doc_hex(ctx, doc, hex_id)
            execute.assert_not_called()
            boxed.assert_called_once_with(ctx, "WriterAgent", "Code cell is empty.")
def test_clear_cell_output_preserves_spacer_before_next_heading():
    """Delete stdout but not the empty paragraph immediately before the next cell."""
    remaining = _walker_scenario(
        [("Preformatted Text", "old stdout"), ("Text Body", ""), ("Heading 2", "1. Creating Arrays")]
    )
    assert remaining == [("Text Body", ""), ("Heading 2", "1. Creating Arrays")]


def _three_cells():
    return [
        new_code_cell_entry(0, None, "nb_cell_0_code"),
        new_code_cell_entry(1, None, "nb_cell_1_code"),
        new_code_cell_entry(2, None, "nb_cell_2_code"),
    ]


def _code_for_field(_doc: object, field_name: str) -> str:
    return {
        "nb_cell_0_code": "x = 1",
        "nb_cell_1_code": "y = x + 1",
        "nb_cell_2_code": "z = y + 1",
    }.get(field_name, "pass")


def test_run_cells_sequence_in_registry_order():
    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    order: list[str] = []

    def _exec(_ctx, _doc, code):
        order.append(code)
        return {"status": "ok", "result": None, "stdout": code}

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle") as flush,
    ):
        result = run_cells(ctx, doc, start_index=0)

    assert result.status == "ok"
    assert result.cells_run == 3
    assert order == ["x = 1", "y = x + 1", "z = y + 1"]
    assert state.next_execution_count == 4
    assert flush.call_count == 2


def test_run_from_here_start_index():
    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    ran: list[str] = []

    def _exec(_ctx, _doc, code):
        ran.append(code)
        return {"status": "ok", "result": None, "stdout": ""}

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
    ):
        result = run_cells(ctx, doc, start_index=1)

    assert result.status == "ok"
    assert result.cells_run == 2
    assert ran == ["y = x + 1", "z = y + 1"]
    assert cells[0].execution_count is None
    assert cells[1].execution_count == 1


def test_find_run_from_here_index_before_first_is_zero():
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells)
    doc = MagicMock()
    text = MagicMock()
    # Selection starts before every cell end (compareRegionStarts(sel, end) == 1).
    text.compareRegionStarts.return_value = 1
    doc.getText.return_value = text
    ends = [MagicMock(name=f"end{i}") for i in range(3)]
    with (
        patch("plugin.notebook.notebook_runner._selection_start_range", return_value=MagicMock()),
        patch("plugin.notebook.notebook_runner._cell_end_range", side_effect=ends),
    ):
        assert find_run_from_here_index(doc, state) == 0


def test_find_run_from_here_index_skips_cells_before_selection():
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells)
    doc = MagicMock()
    text = MagicMock()
    # First two cell ends are before the selection (-1); third is at/after (1).
    text.compareRegionStarts.side_effect = [-1, -1, 1]
    doc.getText.return_value = text
    ends = [MagicMock(name=f"end{i}") for i in range(3)]
    with (
        patch("plugin.notebook.notebook_runner._selection_start_range", return_value=MagicMock()),
        patch("plugin.notebook.notebook_runner._cell_end_range", side_effect=ends),
    ):
        assert find_run_from_here_index(doc, state) == 2


def test_run_cells_stop_skips_remainder():
    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    ran: list[str] = []

    def _exec(_ctx, _doc, code):
        ran.append(code)
        if code == "x = 1":
            request_stop()
        return {"status": "ok", "result": None, "stdout": ""}

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result") as apply,
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
    ):
        result = run_cells(ctx, doc, start_index=0)

    assert result.status == "stopped"
    assert result.cells_run == 1
    assert ran == ["x = 1"]
    assert apply.call_count == 1
    assert cells[1].execution_count is None
    assert cells[2].execution_count is None


def test_run_cells_stop_during_between_cell_drain_skips_next():
    """Stop delivered at flush_ui_idle must not start the next cell."""
    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    ran: list[str] = []

    def _exec(_ctx, _doc, code):
        ran.append(code)
        return {"status": "ok", "result": None, "stdout": ""}

    def _flush(*_a, **_k):
        request_stop()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result") as apply,
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle", side_effect=_flush),
    ):
        result = run_cells(ctx, doc, start_index=0)

    assert result.status == "stopped"
    assert result.cells_run == 1
    assert ran == ["x = 1"]
    assert apply.call_count == 1


def test_run_cells_stop_from_menu_without_active_doc_skips_remainder():
    """Tip: Stop from ≡ menu when get_active_document is None must durably stop Run All."""
    from plugin.notebook.notebook_runner import stop_from_menu

    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    ran: list[str] = []

    def _exec(_ctx, _doc, code):
        ran.append(code)
        return {"status": "ok", "result": None, "stdout": ""}

    def _flush(*_a, **_k):
        # Tip: simulating menu click when active doc is None
        with patch("plugin.framework.uno_context.get_active_document", return_value=None):
            stop_from_menu(ctx, frame=None)

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result") as apply,
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle", side_effect=_flush),
    ):
        result = run_cells(ctx, doc, start_index=0)

    assert result.status == "stopped"
    assert result.cells_run == 1
    assert apply.call_count == 1
    assert ran == ["x = 1"]
    assert cells[1].execution_count is None


def test_run_cells_stop_from_menu_distinct_wrapper_skips_remainder():
    """Tip: Stop from frame with distinct PyUNO wrapper must durably stop the running batch."""
    from plugin.notebook.notebook_runner import stop_from_menu

    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    doc.getRuntimeUID.return_value = ""
    doc.getURL.return_value = ""
    frame = MagicMock()
    other_doc = MagicMock()
    other_doc.getRuntimeUID.return_value = ""
    other_doc.getURL.return_value = ""
    frame.getController.return_value.getModel.return_value = other_doc
    ran: list[str] = []

    def _exec(_ctx, _doc, code):
        ran.append(code)
        return {"status": "ok", "result": None, "stdout": ""}

    def _flush(*_a, **_k):
        stop_from_menu(ctx, frame=frame)

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result") as apply,
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle", side_effect=_flush),
    ):
        result = run_cells(ctx, doc, start_index=0)

    assert result.status == "stopped"
    assert result.cells_run == 1
    assert apply.call_count == 1
    assert ran == ["x = 1"]
    assert cells[1].execution_count is None


def _patch_run_cells(state, exec_side_effect, flush, pump):

    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        with (
            patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
            patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
            patch("plugin.notebook.notebook_runner.execute_code", side_effect=exec_side_effect),
            patch("plugin.notebook.notebook_runner.clear_cell_output"),
            patch("plugin.notebook.notebook_runner.apply_run_result"),
            patch("plugin.notebook.notebook_runner.update_in_prompt"),
            patch("plugin.notebook.notebook_runner.save_registry"),
            patch("plugin.notebook.notebook_runner.flush_ui_idle", new=flush),
            patch("plugin.framework.queue_executor.pump_ui_idle", new=pump),
            patch("plugin.framework.uno_context.get_toolkit", return_value=object()),
        ):
            yield

    return _ctx()


def test_run_cells_chat_stop_under_drain_skips_remainder():
    """Chat Stop during a drain-owned Run All skips cells that have not started.

    flush_ui_idle does not pump while a drain owns VCL. The click is delivered
    by pump_ui_idle, and chat Stop latches SendCancellation rather than the
    notebook Event.
    """
    from plugin.framework.async_drain_guard import drain_owner_scope
    from plugin.framework.send_cancellation import SendCancellation, agent_session

    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    ran: list[str] = []
    scope = SendCancellation()

    def _exec(_ctx, _doc, code):
        ran.append(code)
        return {"status": "ok", "result": None, "stdout": ""}

    def _flush(*_a, **_k):
        scope.cancel()

    with agent_session(scope), drain_owner_scope("stream"):
        with _patch_run_cells(state, _exec, None, _flush):
            result = run_cells(ctx, doc, start_index=0)

    assert result.status == "stopped"
    assert result.cells_run == 1
    assert ran == ["x = 1"]
    assert cells[1].execution_count is None
    assert cells[2].execution_count is None


def test_run_cells_already_cancelled_chat_skips_without_flush():
    """A latched chat Stop skips the batch even when the between-cell flush is a no-op."""
    from plugin.framework.async_drain_guard import drain_owner_scope
    from plugin.framework.send_cancellation import SendCancellation, agent_session

    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    scope = SendCancellation()
    scope.cancel()
    flush = MagicMock()

    def _exec(_ctx, _doc, code):
        raise AssertionError(code)

    with agent_session(scope), drain_owner_scope("stream"):
        with _patch_run_cells(state, _exec, None, flush):
            result = run_cells(ctx, doc, start_index=0)

    assert result.status == "stopped"
    assert result.cells_run == 0
    flush.assert_not_called()


def test_run_cells_busy_guard_skips_play_but_stop_works():
    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    nested: list[object] = []

    def _reenter(*_a, **_k):
        nested.append(run_cell(ctx, doc, cells[1].cell_id))
        request_stop()
        return {"status": "ok", "result": None, "stdout": "1\n"}

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_reenter),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result") as apply,
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
    ):
        result = run_cells(ctx, doc, start_index=0)

    assert result.status == "stopped"
    assert result.cells_run == 1
    assert len(nested) == 1
    assert getattr(nested[0], "status") == "busy"
    assert apply.call_count == 1
    assert cells[1].execution_count is None


@pytest.mark.parametrize("cell_1_value", ["   ", None])
def test_run_cells_skips_empty_and_continues(cell_1_value):
    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    ran: list[str] = []

    def _read(_doc, field_name):
        if field_name == "nb_cell_1_code":
            return cell_1_value
        return _code_for_field(_doc, field_name)

    def _exec(_ctx, _doc, code):
        ran.append(code)
        return {"status": "ok", "result": None, "stdout": ""}

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_read),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result") as apply,
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
    ):
        result = run_cells(ctx, doc, start_index=0)

    assert result.status == "ok"
    assert result.cells_run == 2
    assert ran == ["x = 1", "z = y + 1"]
    assert apply.call_count == 2
    assert cells[1].execution_count is None


def test_run_cells_error_continues_unless_stopped():
    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    apply_status: list[str] = []

    def _exec(_ctx, _doc, code):
        if code == "y = x + 1":
            return {"status": "error", "message": "ValueError", "stdout": "", "traceback": "ValueError"}
        return {"status": "ok", "result": None, "stdout": ""}

    def _apply(_doc, _cell, result, *, ctx=None):
        apply_status.append(str(result.get("status")))

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result", side_effect=_apply),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
        patch("plugin.notebook.notebook_runner.msgbox") as boxed,
    ):
        result = run_cells(ctx, doc, start_index=0)

    boxed.assert_not_called()
    assert result.status == "ok"
    assert result.cells_run == 3
    assert apply_status == ["ok", "error", "ok"]
    assert state.next_execution_count == 4


def _run_cells_with(exec_fn, apply_fn=None, doc=None):
    ctx = MagicMock()
    state = NotebookDocState(code_cells=_three_cells(), next_execution_count=1)
    if doc is None:
        doc = MagicMock()
    applied: list[str] = []

    def _apply(_doc, _cell, result, *, ctx=None):
        applied.append(str(result.get("status")))
        if apply_fn is not None:
            apply_fn(result)

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=exec_fn),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result", side_effect=_apply),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
        patch("plugin.notebook.notebook_runner.msgbox"),
    ):
        result = run_cells(ctx, doc, start_index=0)
    return result, applied, state


def test_run_cells_worker_cancelled_is_a_stop_not_an_error():
    def _exec(_ctx, _doc, code):
        if code == "y = x + 1":
            return {"status": "error", "code": "CANCELLED", "message": "Python worker stopped by user"}
        return {"status": "ok", "result": None, "stdout": ""}

    result, applied, state = _run_cells_with(_exec)
    assert result.status == "stopped"
    assert result.cells_run == 1
    # No traceback written and no In [n] spent on the cancelled cell.
    assert applied == ["ok"]
    assert state.next_execution_count == 2


def test_run_cells_worker_timeout_aborts_the_batch_with_its_message():
    def _exec(_ctx, _doc, code):
        if code == "y = x + 1":
            return {"status": "error", "code": "VENV_TIMEOUT", "message": "Python worker timed out"}
        return {"status": "ok", "result": None, "stdout": ""}

    result, applied, _state = _run_cells_with(_exec)
    assert result.status == "error"
    assert result.message == "Python worker timed out"
    assert result.cells_run == 2
    assert applied == ["ok", "error"]


def test_run_cells_child_timeout_continues_batch():
    def _exec(_ctx, _doc, code):
        if code == "y = x + 1":
            return {
                "status": "error",
                "code": "EXECUTION_TIMEOUT",
                "message": "Code execution exceeded the maximum execution time of 1 seconds",
            }
        return {"status": "ok", "result": None, "stdout": ""}

    result, applied, state = _run_cells_with(_exec)
    assert result.status == "ok"
    assert result.cells_run == 3
    assert applied == ["ok", "error", "ok"]
    assert state.next_execution_count == 4


def test_run_cells_document_disposed_mid_batch_stops_quietly():
    class DisposedException(Exception):
        pass

    def _exec(_ctx, _doc, code):
        return {"status": "ok", "result": None, "stdout": ""}

    seen = {"applies": 0}

    def _apply(_result):
        seen["applies"] += 1
        if seen["applies"] == 2:
            raise DisposedException("document closed")

    result, applied, _state = _run_cells_with(_exec, _apply)
    assert result.status == "stopped"
    assert result.cells_run == 1
    assert applied == ["ok", "ok"]


def test_run_cells_live_document_runtime_exception_propagates():
    class RuntimeException(Exception):
        pass

    def _exec(_ctx, _doc, code):
        return {"status": "ok", "result": None, "stdout": ""}

    def _apply(_result):
        raise RuntimeException("real UNO error")

    with pytest.raises(RuntimeException, match="real UNO error"):
        _run_cells_with(_exec, _apply)


def test_run_cells_document_disposed_mid_batch_value_error_stops_quietly():
    class DisposedException(Exception):
        pass

    def _exec(_ctx, _doc, code):
        return {"status": "ok", "result": None, "stdout": ""}

    seen = {"applies": 0}
    doc = MagicMock()

    def _apply(_result):
        seen["applies"] += 1
        if seen["applies"] == 2:
            doc.getImplementationName.side_effect = DisposedException("document closed")
            raise ValueError("property missing during close")

    result, applied, _state = _run_cells_with(_exec, _apply, doc=doc)
    assert result.status == "stopped"
    assert result.cells_run == 1
    assert applied == ["ok", "ok"]


def test_run_cells_does_not_pump_idle_during_execute():
    ctx = MagicMock()
    cells = _three_cells()[:2]
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    pumps: list[str] = []

    def _exec(*_a, **_k):
        pumps.append("execute")
        return {"status": "ok", "result": None, "stdout": ""}

    def _flush(*_a, **_k):
        pumps.append("flush")

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.framework.queue_executor.pump_main_thread_work_queue", side_effect=_flush),
        patch("plugin.notebook.notebook_runner.flush_ui_idle") as flush_idle,
        patch("plugin.framework.queue_executor.pump_ui_idle"),
    ):
        run_cells(ctx, doc, start_index=0)

    assert pumps == ["execute", "flush", "execute"]
    assert flush_idle.call_count == 1


def test_run_cells_between_cell_pump_under_drain_owner():
    from plugin.notebook.notebook_runner import _pump_between_notebook_cells

    ctx = MagicMock()
    pumps: list[str] = []

    def _flush(*_a, **_k):
        pumps.append("flush")

    def _pump_idle(*_a, **_k):
        pumps.append("pump_ui_idle")

    def _pump_work(*_a, **_k):
        pumps.append("pump_main_thread_work_queue")

    with (
        patch("plugin.framework.async_drain_guard.get_drain_owner", return_value=None),
        patch("plugin.notebook.notebook_runner.flush_ui_idle", side_effect=_flush),
        patch("plugin.framework.queue_executor.pump_ui_idle", side_effect=_pump_idle),
        patch("plugin.framework.queue_executor.pump_main_thread_work_queue", side_effect=_pump_work),
        patch("plugin.framework.uno_context.get_toolkit", return_value=MagicMock()),
    ):
        _pump_between_notebook_cells(ctx)
    assert pumps == ["pump_main_thread_work_queue", "flush"]

    pumps.clear()
    with (
        patch("plugin.framework.async_drain_guard.get_drain_owner", return_value="owner"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle", side_effect=_flush),
        patch("plugin.framework.queue_executor.pump_ui_idle", side_effect=_pump_idle),
        patch("plugin.framework.queue_executor.pump_main_thread_work_queue", side_effect=_pump_work),
        patch("plugin.framework.uno_context.get_toolkit", return_value=MagicMock()),
    ):
        _pump_between_notebook_cells(ctx)
    assert pumps == ["pump_main_thread_work_queue", "pump_ui_idle"]


def test_run_cells_between_cell_pump_disposal_stops_execution():
    ctx = MagicMock()
    cells = _three_cells()[:2]
    state = NotebookDocState(code_cells=cells, next_execution_count=1)
    doc = MagicMock()
    pumps: list[str] = []

    def _exec(*_a, **_k):
        pumps.append("execute")
        return {"status": "ok", "result": None, "stdout": ""}

    def _flush(*_a, **_k):
        pumps.append("flush")

    with (
        patch("plugin.framework.async_drain_guard.get_drain_owner", return_value=None),
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", side_effect=_code_for_field),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle", side_effect=_flush),
        patch("plugin.notebook.notebook_runner.document_is_disposed", side_effect=[False, True]),
    ):
        result = run_cells(ctx, doc, start_index=0)

    # Second cell skipped because document was disposed after first cell
    assert pumps == ["execute", "flush"]
    assert result.status == "stopped"


def test_run_all_for_doc_hex_setup_msgbox_only():
    from plugin.notebook.notebook_runner import run_all_for_doc

    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.notebook.notebook_runner.is_writer", return_value=False),
        patch("plugin.notebook.notebook_runner.msgbox") as boxed,
        patch("plugin.notebook.notebook_runner.run_cells") as run,
    ):
        assert run_all_for_doc(ctx, doc) is None
    boxed.assert_called_once()
    run.assert_not_called()


def test_run_cells_reuses_single_draw_page_walk_and_matches_unscoped():
    ctx = MagicMock()
    cells = _three_cells()
    state = NotebookDocState(code_cells=cells, next_execution_count=1)

    get_by_index_calls = []

    def make_shape(name, text):
        model = MagicMock()
        model.Name = name
        model.Text = text
        shape = MagicMock()
        shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
        shape.Control = model
        return shape

    shapes = [
        make_shape("nb_cell_0_code", "print('hello 0')"),
        make_shape("nb_cell_1_code", "print('hello 1')"),
        make_shape("nb_cell_2_code", "print('hello 2')"),
    ]

    dp = MagicMock()
    dp.getCount.return_value = len(shapes)

    def _get_by_index(idx):
        get_by_index_calls.append(idx)
        return shapes[idx]

    dp.getByIndex.side_effect = _get_by_index

    doc = MagicMock()
    doc.getURL.return_value = "file:///tmp/nb.odt"
    doc.getRuntimeUID.return_value = "uid-scope-test"
    doc.getDrawPage.return_value = dp

    outputs_scoped = []

    def _apply_scoped(_doc, _cell, result, **_k):
        outputs_scoped.append(result.get("stdout"))

    def _exec(_ctx, _doc, code):
        return {"status": "ok", "result": None, "stdout": f"out: {code}"}

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.notebook_runner.execute_code", side_effect=_exec),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result", side_effect=_apply_scoped),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
    ):
        result = run_cells(ctx, doc)

    assert result.status == "ok"
    assert len(get_by_index_calls) == len(shapes)

    outputs_unscoped = []
    for cell in cells:
        code = form_lookup.read_code_from_field(doc, cell.code_field_name)
        res = _exec(ctx, doc, code)
        outputs_unscoped.append(res.get("stdout"))

    assert outputs_scoped == outputs_unscoped
    assert outputs_scoped == [
        "out: print('hello 0')",
        "out: print('hello 1')",
        "out: print('hello 2')",
    ]


def test_request_stop_while_idle_creates_no_state():
    from plugin.notebook import notebook_runner

    notebook_runner._stop_flags.clear()
    request_stop()
    assert notebook_runner._stop_flags == {}


def test_run_cell_leaves_no_stop_state():
    from plugin.notebook import notebook_runner

    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=1)
    doc = MagicMock()
    doc.getURL.return_value = ""
    notebook_runner._stop_flags.clear()
    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.form_lookup.read_code_from_field", return_value="print(1)"),
        patch("plugin.notebook.notebook_runner.execute_code", return_value={"status": "ok", "result": None, "stdout": "1\n"}),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
    ):
        assert run_cell(ctx, doc, cell.cell_id).status == "ok"
    assert notebook_runner._stop_flags == {}

