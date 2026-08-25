# WriterAgent - tests for notebook cell execution

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from plugin.notebook.cell_registry import NotebookDocState, cell_id_to_hex, new_code_cell_entry
from plugin.notebook.notebook_runner import (
    _find_cell_output_heading_end,
    _gutter_text_cursor,
    _is_next_cell_boundary,
    _paragraph_string,
    apply_run_result,
    clear_cell_output,
    execute_code,
    format_run_output_text,
    init_registry_execution_counter,
    read_code_from_field,
    run_cell,
    update_in_prompt,
    run_cell_target_url,
)
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_format_run_output_text_stdout_and_result():
    text = format_run_output_text({"status": "ok", "stdout": "hi\n", "result": 42})
    assert "hi" in text
    assert "42" in text


def test_format_run_output_text_error_traceback():
    text = format_run_output_text(
        {"status": "error", "traceback": "\x1b[31mValueError\x1b[0m: bad", "stdout": ""}
    )
    assert "ValueError" in text
    assert "\x1b" not in text


def test_format_run_output_text_skips_image_result():
    wire = {"__wa_payload__": "image", "data": b"x", "format": "png"}
    with patch("plugin.notebook.notebook_runner.is_image_payload", return_value=True):
        text = format_run_output_text({"status": "ok", "result": wire, "stdout": ""})
    assert text == ""


def test_read_code_from_field_finds_textfield():
    field_model = MagicMock()
    field_model.Name = "nb_cell_0_code"
    field_model.Text = "x = 1\n"

    portion = MagicMock()
    portion.getPropertyValue.return_value = "Frame"
    portion.TextField = field_model

    para = MagicMock()
    para.createEnumeration.return_value = _enum_of([portion])

    doc = MagicMock()
    doc.getText.return_value.createEnumeration.return_value = _enum_of([para])

    assert read_code_from_field(doc, "nb_cell_0_code") == "x = 1\n"


def _enum_of(items):
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True] * len(items) + [False]
    enum.nextElement.side_effect = items
    return enum


def test_run_cell_target_url():
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    url = run_cell_target_url(cell.cell_id)
    assert url == f"org.extension.writeragent:notebook.run_cell.{cell_id_to_hex(cell.cell_id)}"


def test_init_registry_execution_counter():
    c0 = new_code_cell_entry(0, 3, "nb_cell_0_code")
    c1 = new_code_cell_entry(1, None, "nb_cell_1_code")
    state = NotebookDocState(code_cells=[c0, c1])
    init_registry_execution_counter(state)
    assert state.next_execution_count == 1


def test_execute_code_uses_blocking_pump():
    ctx = MagicMock()
    doc = MagicMock()
    worker_result = {"status": "ok", "result": 1, "stdout": ""}

    with (
        patch("plugin.notebook.notebook_runner.notebook_session_id", return_value="notebook:test"),
        patch("plugin.notebook.notebook_runner.run_blocking_in_thread", return_value=worker_result) as pump,
        patch("plugin.notebook.notebook_runner.run_code_in_user_venv") as run_venv,
    ):
        out = execute_code(ctx, doc, "x = 1")
        assert out == worker_result
        pump.assert_called_once()
        run_venv.assert_not_called()


def test_run_cell_updates_registry_and_execution_count():
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=5)
    doc = MagicMock()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.notebook_runner.read_code_from_field", return_value="print(1)"),
        patch(
            "plugin.notebook.notebook_runner.execute_code",
            return_value={"status": "ok", "result": None, "stdout": "1\n"},
        ),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry") as save_reg,
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
    ):
        result = run_cell(ctx, doc, cell.cell_id)

    assert result.status == "ok"
    assert result.execution_count == 5
    assert cell.execution_count == 5
    assert state.next_execution_count == 6
    save_reg.assert_called_once_with(doc, state)


def test_run_cell_logs_status_after_execute():
    src = inspect.getsource(run_cell)
    assert "status=%s" in src
    assert src.find("execute_code(") < src.find("status=%s")


def test_run_cell_empty_code():
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell])
    doc = MagicMock()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.notebook_runner.read_code_from_field", return_value="   "),
    ):
        result = run_cell(ctx, doc, cell.cell_id)
    assert result.status == "error"
    assert "empty" in result.message.lower()


def test_insert_run_image_svg_mime():
    from plugin.notebook.notebook_runner import _insert_run_image

    doc = MagicMock()
    payload = {"__wa_payload__": "image", "format": "svg", "data": b"<svg></svg>"}
    with patch("plugin.notebook.notebook_runner._insert_image_in_flow", return_value=True) as insert_flow:
        assert _insert_run_image(doc, payload, ctx=MagicMock(), images_before=0) is True
    insert_flow.assert_called_once()
    assert insert_flow.call_args.kwargs["mime"] == "image/svg+xml"


def test_shared_notebook_session_via_sandbox():
    from plugin.scripting.venv.venv_sandbox import clear_all_sandbox_sessions
    from plugin.scripting.venv.worker_harness import _execute_request

    clear_all_sandbox_sessions()
    sid = "notebook:test-runner"
    r1 = _execute_request("x = 41\nresult = x + 1", None, session_id=sid)
    assert r1["status"] == "ok"
    r2 = _execute_request("result = x + 1", None, session_id=sid)
    assert r2["status"] == "ok"
    assert r2["result"] == 42
    clear_all_sandbox_sessions()


@pytest.mark.parametrize(
    "hex_id,expected_ok",
    [
        ("abc", False),
        ("0" * 32, True),
    ],
)
def test_cell_id_hex_round_trip(hex_id, expected_ok):
    from plugin.notebook.cell_registry import cell_id_from_hex

    restored = cell_id_from_hex(hex_id)
    if not expected_ok:
        assert restored is None
        return
    assert restored is not None
    assert cell_id_to_hex(restored) == hex_id


def test_clear_cell_output_source_has_no_delete_contents():
    src = inspect.getsource(clear_cell_output)
    assert ".deleteContents(" not in src
    assert "setString" in src


def test_find_output_heading_stays_inside_paragraph():
    """para.getEnd() is the paragraph break; a bookmark there is deleted by setString."""
    src = inspect.getsource(_find_cell_output_heading_end)
    assert "createTextCursorByRange(para.getEnd())" not in src
    assert "para.getStart()" in src
    assert "goRight" in src


def test_find_output_heading_skips_earlier_markdown_chrome():
    """``Cell 1: Markdown`` is a boundary but must not abort before this cell's Output."""

    def _para(text: str, style: str = "Heading 3") -> MagicMock:
        para = MagicMock()
        para.getString.return_value = text
        para.getPropertyValue.return_value = style
        para.supportsService.return_value = True
        para.getStart.return_value = f"start:{text}"
        return para

    output_para = _para("Output", "Heading 4")
    paras = [
        _para("Cell 1: Markdown"),
        _para("Before code", "Heading 1"),
        _para("[In [1]]\tCell 2: Code", "WriterAgent Notebook In"),
        _para("", "WriterAgent Notebook In"),
        output_para,
        _para("Cell 3: Markdown"),
    ]
    heading_cursor = MagicMock(name="inside-output")
    heading_cursor.goRight.return_value = True
    text = MagicMock()
    text.createEnumeration.return_value = _enum_of(paras)
    text.createTextCursorByRange.return_value = heading_cursor
    doc = MagicMock()
    doc.getText.return_value = text
    cell = new_code_cell_entry(1, None, "nb_cell_1_code")
    with patch("plugin.notebook.notebook_runner._resolve_para_style", return_value="WriterAgent Notebook In"):
        found = _find_cell_output_heading_end(doc, cell)
    assert found is heading_cursor
    text.createTextCursorByRange.assert_called_with(output_para.getStart())
    heading_cursor.goRight.assert_called_once_with(6, False)


def test_is_next_cell_boundary_markdown_and_code():
    assert _is_next_cell_boundary("Heading 3", "Cell 3: Markdown", None) is True
    assert _is_next_cell_boundary("Heading 3", "Cell 5: Raw", None) is True
    assert _is_next_cell_boundary("Preformatted Text", "old stdout", None) is False
    assert _is_next_cell_boundary("WriterAgent Notebook In", "[In [1]]\tCell 2: Code", "WriterAgent Notebook In") is True
    assert _is_next_cell_boundary("WriterAgent Notebook In", "", "WriterAgent Notebook In") is False


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


def test_clear_cell_output_uses_set_string_not_delete_contents():
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    start = MagicMock(name="start")
    start.ParaStyleName = "Preformatted Text"
    start.getString.return_value = "old stdout"

    walker = MagicMock(name="walker")
    walker.ParaStyleName = "Preformatted Text"
    walker.getString.return_value = "old stdout"

    def goto_next(_expand):
        walker.ParaStyleName = "WriterAgent Notebook In"
        walker.getString.return_value = "[In [ ]]\tCell 2: Code"
        return True

    walker.gotoNextParagraph.side_effect = goto_next
    walker.getStart.return_value = "end-pos"

    range_start = MagicMock(name="range_start")
    sel = MagicMock(name="sel")
    sel.getString.return_value = "old stdout\n"

    text = MagicMock()
    text.createTextCursorByRange.side_effect = [walker, range_start, sel]
    doc = MagicMock()
    doc.getText.return_value = text

    with (
        patch("plugin.notebook.notebook_runner._cursor_after_bookmark", return_value=start),
        patch("plugin.notebook.notebook_runner._resolve_para_style", return_value="WriterAgent Notebook In"),
    ):
        clear_cell_output(doc, cell)

    text.deleteContents.assert_not_called()
    sel.gotoRange.assert_called_once_with("end-pos", True)
    sel.setString.assert_called_once_with("")


def test_clear_cell_output_stops_at_markdown_cell_heading():
    cell = new_code_cell_entry(1, None, "nb_cell_1_code")
    start = MagicMock(name="start")
    start.ParaStyleName = "Preformatted Text"
    start.getString.return_value = "Array: [10 20 30]"

    walker = MagicMock(name="walker")
    walker.ParaStyleName = "Preformatted Text"
    walker.getString.return_value = "Array: [10 20 30]"

    def goto_next(_expand):
        walker.ParaStyleName = "Heading 3"
        walker.getString.return_value = "Cell 3: Markdown"
        return True

    walker.gotoNextParagraph.side_effect = goto_next
    walker.getStart.return_value = "md-start"

    range_start = MagicMock(name="range_start")
    sel = MagicMock(name="sel")
    sel.getString.return_value = "Array: [10 20 30]\n"

    text = MagicMock()
    text.createTextCursorByRange.side_effect = [walker, range_start, sel]
    doc = MagicMock()
    doc.getText.return_value = text

    with (
        patch("plugin.notebook.notebook_runner._cursor_after_bookmark", return_value=start),
        patch("plugin.notebook.notebook_runner._resolve_para_style", return_value="WriterAgent Notebook In"),
    ):
        clear_cell_output(doc, cell)

    sel.setString.assert_called_once_with("")
    walker.gotoStartOfParagraph.assert_called_once()


def test_clear_cell_output_collapsed_cursor_stops_at_markdown():
    """Live Writer collapsed cursors return empty getString(); expand to find chrome."""
    cell = new_code_cell_entry(1, None, "nb_cell_1_code")
    start = MagicMock(name="start")
    start.ParaStyleName = "Preformatted Text"
    start.getString.return_value = ""

    walker = MagicMock(name="walker")
    walker.ParaStyleName = "Preformatted Text"
    walker.getString.return_value = ""
    para_text = {"v": "old stdout"}
    probe = MagicMock()
    probe.getString.side_effect = lambda: para_text["v"]
    walker.getText.return_value.createTextCursorByRange.return_value = probe

    def goto_next(_expand):
        walker.ParaStyleName = "Heading 3"
        para_text["v"] = "Cell 3: Markdown"
        return True

    walker.gotoNextParagraph.side_effect = goto_next
    walker.getStart.return_value = "md-start"

    range_start = MagicMock(name="range_start")
    sel = MagicMock(name="sel")
    sel.getString.return_value = "old stdout\n"

    text = MagicMock()
    text.createTextCursorByRange.side_effect = [walker, range_start, sel]
    doc = MagicMock()
    doc.getText.return_value = text

    with (
        patch("plugin.notebook.notebook_runner._cursor_after_bookmark", return_value=start),
        patch("plugin.notebook.notebook_runner._resolve_para_style", return_value="WriterAgent Notebook In"),
    ):
        clear_cell_output(doc, cell)

    sel.setString.assert_called_once_with("")
    walker.gotoStartOfParagraph.assert_called_once()


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
        patch("plugin.notebook.notebook_runner.read_code_from_field", return_value="print(1)"),
        patch(
            "plugin.notebook.notebook_runner.execute_code",
            return_value={"status": "ok", "result": None, "stdout": "1\n"},
        ),
        patch("plugin.notebook.notebook_runner.clear_cell_output", side_effect=rec_clear),
        patch("plugin.notebook.notebook_runner.apply_run_result", side_effect=rec_apply),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
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
        patch("plugin.notebook.notebook_runner._resolve_para_style", side_effect=lambda _doc, name: name),
    ):
        apply_run_result(doc, cell, {"status": "ok", "stdout": "first\n", "result": None})
        apply_run_result(doc, cell, {"status": "ok", "stdout": "second\n", "result": None})

    assert [call.args[1] for call in text.insertString.call_args_list] == ["first", "second"]
    text.deleteContents.assert_not_called()
    cursor.gotoEnd.assert_not_called()
    text.insertControlCharacter.assert_not_called()


def test_insert_stdout_paragraph_splits_before_next_cell_chrome():
    """When the cursor lands on Cell N: Markdown, stdout must not replace that heading."""
    from plugin.notebook.notebook_runner import _insert_stdout_paragraph
    from plugin.notebook.writer_importer import _PARAGRAPH_BREAK

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
        _PARAGRAPH_BREAK,
        _PARAGRAPH_BREAK,
    ]
    text.insertString.assert_called_once_with(cursor, "hello", False)
    cursor.setString.assert_not_called()
    cursor.goRight.assert_called()


def test_gutter_text_cursor_stops_before_frame_portion():
    """setString must not cover AS_CHARACTER ControlShapes (▶ / code field)."""
    text_portion = MagicMock(name="text_portion")
    text_portion.getPropertyValue.return_value = "Text"
    frame_portion = MagicMock(name="frame_portion")
    frame_portion.getPropertyValue.return_value = "Frame"

    para = MagicMock()
    para.createEnumeration.return_value = _enum_of([text_portion, frame_portion])

    text_cursor = MagicMock(name="text_cursor")
    text = MagicMock()
    text.createTextCursorByRange.return_value = text_cursor

    cursor = _gutter_text_cursor(text, para)
    assert cursor is text_cursor
    text.createTextCursorByRange.assert_called_once_with(text_portion)
    text_cursor.gotoRange.assert_called_once_with(text_portion, True)
    assert all(call.args[0] is not frame_portion for call in text.createTextCursorByRange.call_args_list)


def test_update_in_prompt_does_not_setstring_whole_paragraph():
    """Whole-para setString deletes in-flow ▶ and code fields that share the gutter line."""
    text_portion = MagicMock(name="text_portion")
    text_portion.getPropertyValue.return_value = "Text"
    frame_portion = MagicMock(name="frame_portion")
    frame_portion.getPropertyValue.return_value = "Frame"

    para = MagicMock()
    para.getString.return_value = "[In [1]]\tCell 2: Code"
    para.createEnumeration.return_value = _enum_of([text_portion, frame_portion])
    para.getStart.return_value = "para-start"
    para.getEnd.return_value = "para-end"

    text_cursor = MagicMock(name="text_cursor")
    whole_para = MagicMock(name="whole_para")
    text = MagicMock()
    text.createEnumeration.return_value = _enum_of([para])

    def _cursor_for(rng):
        if rng is text_portion:
            return text_cursor
        return whole_para

    text.createTextCursorByRange.side_effect = _cursor_for
    doc = MagicMock()
    doc.getText.return_value = text

    cell = new_code_cell_entry(1, 1, "nb_cell_1_code")
    update_in_prompt(doc, cell, 4)

    text_cursor.setString.assert_called_once_with("[In [4]]\tCell 2: Code")
    whole_para.setString.assert_not_called()
    whole_para.gotoRange.assert_not_called()


def test_update_in_prompt_source_never_setstrings_para_end():
    src = inspect.getsource(update_in_prompt)
    assert "para.getEnd()" not in src
    assert "_gutter_text_cursor" in src


def test_run_cell_error_still_increments_execution_count():
    """Failed runs consume a kernel count, matching Jupyter In [n]."""
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=1)
    doc = MagicMock()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.notebook_runner.read_code_from_field", return_value="raise ValueError(1)"),
        patch(
            "plugin.notebook.notebook_runner.execute_code",
            return_value={"status": "error", "message": "ValueError", "stdout": ""},
        ),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry"),
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
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
    src = inspect.getsource(apply_run_result)
    assert "_append_body_text_block" not in src

    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    doc = MagicMock()
    text = MagicMock()
    doc.getText.return_value = text
    with (
        patch("plugin.notebook.notebook_runner._reanchor_output_bookmark", return_value=None),
        patch("plugin.notebook.notebook_runner._cursor_after_bookmark", return_value=None),
        patch("plugin.notebook.notebook_runner._find_cell_output_heading_end", return_value=None),
        patch("plugin.notebook.notebook_runner._insert_stdout_paragraph") as insert,
    ):
        apply_run_result(doc, cell, {"status": "ok", "stdout": "dumped\n", "result": None})
    insert.assert_not_called()
    text.insertString.assert_not_called()
    text.insertControlCharacter.assert_not_called()
