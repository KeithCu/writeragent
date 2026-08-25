# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live Writer: fire notebook ▶ via ``getControl`` / ``XButton``, not ``run_cell()`` alone.

Confirms a successful run leaves ``nb_run_*`` and ``nb_cell_*_code`` on the draw
page, writes stdout as its own paragraph, and a re-click replaces output without
eating the following markdown heading.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from plugin.testing_runner import native_test, show_window
from plugin.tests.testing_utils import with_native_doc

_SENTINEL = "WA_NB_SENTINEL"
_AFTER_HEADING = "After code heading"


def _tiny_ipynb_path() -> Path:
    payload = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": "# Before code\n"},
            {
                "cell_type": "code",
                "metadata": {},
                "execution_count": None,
                "outputs": [],
                "source": f"print({_SENTINEL!r})\n",
            },
            {"cell_type": "markdown", "metadata": {}, "source": f"## {_AFTER_HEADING}\n"},
        ],
    }
    handle = tempfile.NamedTemporaryFile(suffix=".ipynb", delete=False, mode="w", encoding="utf-8")
    with handle as fh:
        json.dump(payload, fh)
    return Path(handle.name)


def _paragraphs(doc) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        try:
            if hasattr(el, "supportsService") and not el.supportsService("com.sun.star.text.Paragraph"):
                continue
            style = str(el.getPropertyValue("ParaStyleName") or "")
            text = str(el.getString() or "")
        except Exception:
            continue
        out.append((style, text))
    return out


def _draw_control_names(doc) -> list[str]:
    names: list[str] = []
    dp = doc.getDrawPage()
    for i in range(dp.getCount()):
        shape = dp.getByIndex(i)
        try:
            if shape.getShapeType() != "com.sun.star.drawing.ControlShape":
                continue
            name = str(getattr(shape.Control, "Name", "") or "")
        except Exception:
            continue
        if name:
            names.append(name)
    return names


def _assert_controls_present(doc, cell) -> None:
    from plugin.notebook.cell_registry import cell_id_to_hex

    names = _draw_control_names(doc)
    run_name = f"nb_run_{cell_id_to_hex(cell.cell_id)}"
    assert run_name in names, f"{run_name} missing from draw page: {names}"
    assert cell.code_field_name in names, f"{cell.code_field_name} missing from draw page: {names}"


def _assert_stdout_not_mashed(doc) -> list[tuple[str, str]]:
    paras = _paragraphs(doc)
    sentinel_paras = [t for _s, t in paras if _SENTINEL in t]
    assert sentinel_paras, f"stdout {_SENTINEL!r} missing from body: {paras!r}"
    for text in sentinel_paras:
        assert "Cell 3: Markdown" not in text, f"stdout mashed onto next heading: {text!r}"
        assert _AFTER_HEADING not in text, f"stdout mashed onto following markdown: {text!r}"
    chrome = [t for _s, t in paras if "Cell 3: Markdown" in t]
    assert chrome, f"Cell 3: Markdown heading missing after run: {paras!r}"
    for text in chrome:
        assert _SENTINEL not in text, f"next-cell chrome contains stdout: {text!r}"
    assert any(t.strip() == "Output" for _s, t in paras), f"Output heading split or missing: {paras!r}"
    return paras


def _assert_stdout_own_paragraph(doc) -> None:
    _assert_stdout_not_mashed(doc)
    body = doc.getText().getString() or ""
    assert _AFTER_HEADING in body
    assert "Before code" in body


def _fire_run_button_via_get_control(_ctx, doc, hex_id: str) -> str:
    """Click ▶ through the live control view. Returns how the click was delivered."""
    import uno

    from plugin.notebook.form_lookup import find_form_control_model_by_name
    from plugin.notebook.notebook_controls import (
        _listener_refs,
        _query_interface,
        get_control_view_for_model,
        wired_run_listener_count,
    )

    model = find_form_control_model_by_name(doc, f"nb_run_{hex_id}")
    assert model is not None, f"no form model nb_run_{hex_id}"
    control = get_control_view_for_model(doc, model)
    assert control is not None, "getControl returned no live view for ▶"
    btn = _query_interface(control, "com.sun.star.awt.XButton")
    assert btn is not None, "live view is not XButton"

    # XAccessibleAction.doAccessibleAction is delivered on a VCL worker
    # (Dummy-1). Dev UNO thread guard then aborts run_cell before output.
    # Fire the live XActionListener on this (main) thread instead.
    evt = uno.createUnoStruct("com.sun.star.awt.ActionEvent")
    evt.Source = control
    evt.ActionCommand = str(getattr(model, "Name", "") or "")
    n = wired_run_listener_count(hex_id)
    assert n == 1, f"expected 1 XActionListener for ▶, got {n} (triplicate wiring?)"
    matched = [lis for lis in _listener_refs if getattr(lis, "_hex_id", None) == hex_id]
    assert len(matched) == 1, f"listener list mismatch: {len(matched)}"
    # Fire every matched listener — a real click would. With n==1 this is one run.
    for lis in matched:
        lis.actionPerformed(evt)
    return "action-listener"


@native_test
@with_native_doc("writer", hidden=not show_window)
def test_run_button_getcontrol_keeps_controls_and_splits_output(ctx, doc):
    from plugin.notebook.cell_registry import cell_id_to_hex, load_registry
    from plugin.notebook.notebook_controls import (
        ensure_form_design_mode_off,
        wire_all_notebook_run_buttons,
    )
    from plugin.notebook.notebook_runner import read_code_from_field
    from plugin.notebook.writer_importer import import_ipynb_to_writer, flush_ui_idle

    ipynb = _tiny_ipynb_path()
    try:
        import_ipynb_to_writer(doc, str(ipynb), ctx=ctx)
        flush_ui_idle(ctx)

        state = load_registry(doc)
        assert state is not None and len(state.code_cells) == 1
        cell = state.code_cells[0]
        src = read_code_from_field(doc, cell.code_field_name)
        assert _SENTINEL in src
        _assert_controls_present(doc, cell)

        ensure_form_design_mode_off(doc)
        wired = wire_all_notebook_run_buttons(ctx, doc)
        assert wired == 1, f"expected wired 1/1 run button, got {wired}"

        boxes: list = []

        def _capture(c, title, message, *, box_type=1):
            boxes.append((str(title), str(message), box_type))

        hex_id = cell_id_to_hex(cell.cell_id)
        fake_result = {"status": "ok", "stdout": f"{_SENTINEL}\n", "result": None}
        with (
            patch("plugin.notebook.notebook_runner.msgbox", _capture),
            patch("plugin.notebook.notebook_runner.execute_code", return_value=fake_result),
        ):
            how = _fire_run_button_via_get_control(ctx, doc, hex_id)
        print(f"notebook ▶ delivered via {how}; msgboxes={boxes!r} paras={_paragraphs(doc)!r}", flush=True)
        flush_ui_idle(ctx)

        _assert_controls_present(doc, cell)
        _assert_stdout_own_paragraph(doc)
        assert all("empty" not in msg.lower() for _t, msg, _b in boxes), boxes

        before_count = sum(1 for _s, t in _paragraphs(doc) if _SENTINEL in t)
        with (
            patch("plugin.notebook.notebook_runner.msgbox", _capture),
            patch("plugin.notebook.notebook_runner.execute_code", return_value=fake_result),
        ):
            _fire_run_button_via_get_control(ctx, doc, hex_id)
        flush_ui_idle(ctx)

        _assert_controls_present(doc, cell)
        _assert_stdout_own_paragraph(doc)
        after_count = sum(1 for _s, t in _paragraphs(doc) if _SENTINEL in t)
        assert after_count == 1, (
            f"re-click appended stdout paras: before={before_count} after={after_count} "
            f"paras={_paragraphs(doc)!r}"
        )
    finally:
        try:
            ipynb.unlink()
        except OSError:
            pass


@native_test
@with_native_doc("writer", hidden=not show_window)
def test_apply_run_result_stdout_is_own_paragraph(ctx, doc):
    """Live Writer: stdout under Output must not concatenate onto the next cell heading."""
    from plugin.notebook.cell_registry import insert_output_start_bookmark, new_code_cell_entry
    from plugin.notebook.notebook_runner import apply_run_result, clear_cell_output
    from plugin.notebook.writer_importer import (
        _STYLE_CELL_HEADING,
        _STYLE_MD_H2,
        _STYLE_SECTION_HEADING,
        _append_body_paragraph,
    )

    _append_body_paragraph(doc, "Output", _STYLE_SECTION_HEADING, lead_break=False)
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    insert_output_start_bookmark(doc, cell.output_start_bookmark)
    _append_body_paragraph(doc, "Cell 3: Markdown", _STYLE_CELL_HEADING, lead_break=True)
    _append_body_paragraph(doc, _AFTER_HEADING, _STYLE_MD_H2, lead_break=True)

    apply_run_result(doc, cell, {"status": "ok", "stdout": f"{_SENTINEL}\n", "result": None}, ctx=ctx)
    _assert_stdout_not_mashed(doc)
    assert _AFTER_HEADING in (doc.getText().getString() or "")

    clear_cell_output(doc, cell)
    apply_run_result(doc, cell, {"status": "ok", "stdout": f"{_SENTINEL}\n", "result": None}, ctx=ctx)
    _assert_stdout_not_mashed(doc)
    assert sum(1 for _s, t in _paragraphs(doc) if _SENTINEL in t) == 1
    assert _AFTER_HEADING in (doc.getText().getString() or "")
    assert "Cell 3: Markdown" in (doc.getText().getString() or "")
    assert _empty_paras_between_output_and_content(doc) <= 1


_SMALL_IPYNB = Path(__file__).resolve().parents[1] / "fixtures" / "introduction-to-numpy-small.ipynb"


def _empty_paras_between_output_and_content(doc) -> int:
    """Blank paragraphs after the first Output heading before stdout or the next cell."""
    seen_output = False
    empties = 0
    for _style, text in _paragraphs(doc):
        if not seen_output:
            if text.strip() == "Output":
                seen_output = True
            continue
        stripped = text.strip()
        if stripped.startswith("Cell ") or stripped.startswith("[In ["):
            break
        if not stripped:
            empties += 1
            continue
        break
    return empties


def _output_text_for_cell(doc, cell) -> str:
    from plugin.notebook.notebook_runner import (
        _cursor_after_bookmark,
        _is_next_cell_boundary,
        _paragraph_string,
    )
    from plugin.notebook.writer_importer import _STYLE_NOTEBOOK_IN, _resolve_para_style

    start = _cursor_after_bookmark(doc, cell.output_start_bookmark)
    if start is None:
        return ""
    text = doc.getText()
    notebook_in = _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)
    end = text.createTextCursorByRange(start)
    while end.gotoNextParagraph(False):
        if _is_next_cell_boundary(end.ParaStyleName, _paragraph_string(end), notebook_in):
            end.gotoStartOfParagraph(False)
            break
    else:
        end.gotoEnd(False)
    sel = text.createTextCursorByRange(start)
    sel.gotoRange(end.getStart(), True)
    return str(sel.getString() or "")


def _gutter_line_for_cell(doc, cell_index: int) -> str:
    marker = f"Cell {cell_index + 1}: Code"
    for _style, text in _paragraphs(doc):
        if marker in text:
            return text
    return ""


@native_test
@with_native_doc("writer", hidden=not show_window)
def test_small_numpy_button_rerun_stays_in_cell_and_counts_from_one(ctx, doc):
    """Live ▶ on the small NumPy fixture: in-cell replace, tight Output, In [1] then [2]."""
    assert _SMALL_IPYNB.is_file(), f"missing fixture {_SMALL_IPYNB}"

    from plugin.notebook.cell_registry import cell_id_to_hex, load_registry
    from plugin.notebook.notebook_controls import (
        ensure_form_design_mode_off,
        wire_all_notebook_run_buttons,
        wired_run_listener_count,
    )
    from plugin.notebook.writer_importer import import_ipynb_to_writer, flush_ui_idle

    boxes: list = []

    def _capture(_c, title, message, *, box_type=1):
        boxes.append((str(title), str(message), box_type))

    with patch("plugin.notebook.notebook_runner.msgbox", _capture):
        import_ipynb_to_writer(doc, str(_SMALL_IPYNB), ctx=ctx)
        flush_ui_idle(ctx)

        state = load_registry(doc)
        assert state is not None and len(state.code_cells) == 3
        assert state.next_execution_count == 1, (
            f"new kernel must start at 1, not max(saved)+1; got {state.next_execution_count}"
        )
        cell = state.code_cells[0]
        hex_id = cell_id_to_hex(cell.cell_id)
        bm_name = cell.output_start_bookmark
        assert doc.getBookmarks().hasByName(bm_name), f"missing {bm_name}"

        ensure_form_design_mode_off(doc)
        wire_all_notebook_run_buttons(ctx, doc)
        wire_all_notebook_run_buttons(ctx, doc)
        assert wired_run_listener_count(hex_id) == 1, (
            f"extra wire_all attached duplicate listeners: {wired_run_listener_count(hex_id)}"
        )
        _assert_controls_present(doc, cell)

        fake_result = {"status": "ok", "stdout": "NumPy Version: 2.5.2\n", "result": None}
        with patch("plugin.notebook.notebook_runner.execute_code", return_value=fake_result):
            _fire_run_button_via_get_control(ctx, doc, hex_id)
            flush_ui_idle(ctx)
            state = load_registry(doc)
            assert state is not None
            cell = state.code_cells[0]
            assert cell.execution_count == 1, f"first live run must be In [1], got {cell.execution_count}"
            assert state.next_execution_count == 2
            assert "[In [1]]" in _gutter_line_for_cell(doc, cell.index)
            assert doc.getBookmarks().hasByName(bm_name), "bookmark vanished after first run"
            out1 = _output_text_for_cell(doc, cell)
            body = doc.getText().getString() or ""
            print(
                f"first ▶ status_out={out1!r} gap={_empty_paras_between_output_and_content(doc)} "
                f"paras={_paragraphs(doc)!r} boxes={boxes!r}",
                flush=True,
            )
            assert out1.strip(), f"first ▶ produced no in-cell output: {out1!r} boxes={boxes!r}"
            assert _empty_paras_between_output_and_content(doc) <= 1, (
                f"extra blank paras under Output: {_paragraphs(doc)!r}"
            )
            assert "Cell 3: Markdown" in body
            assert "1. Creating Arrays" in body
            assert any(t.strip() == "Output" for _s, t in _paragraphs(doc)), (
                f"Output heading split or missing: {_paragraphs(doc)!r}"
            )
            _assert_controls_present(doc, cell)
            needle = "NumPy Version"
            assert needle in out1, f"expected in-cell stdout, got {out1!r}"
            assert body.count(needle) == 1, f"stdout dumped outside the cell: tail={body[-400:]!r}"

            _fire_run_button_via_get_control(ctx, doc, hex_id)
            flush_ui_idle(ctx)
            state = load_registry(doc)
            assert state is not None
            cell = state.code_cells[0]
            assert cell.execution_count == 2, f"re-click must increment by 1, got {cell.execution_count}"
            assert state.next_execution_count == 3
            assert "[In [2]]" in _gutter_line_for_cell(doc, cell.index)
            assert doc.getBookmarks().hasByName(bm_name), "bookmark vanished after re-click"
            out2 = _output_text_for_cell(doc, cell)
            body2 = doc.getText().getString() or ""
            print(f"second ▶ out={out2!r} tail={body2[-400:]!r} paras={_paragraphs(doc)!r}", flush=True)
            assert out2.count(needle) == 1, f"re-click duplicated in-cell stdout: {out2!r}"
            assert body2.count(needle) == 1, f"re-click appended at document end: {body2[-400:]!r}"
            assert "Cell 3: Markdown" in body2
            assert "1. Creating Arrays" in body2
            _assert_controls_present(doc, cell)
            assert _empty_paras_between_output_and_content(doc) <= 1

            _fire_run_button_via_get_control(ctx, doc, hex_id)
            flush_ui_idle(ctx)
            state = load_registry(doc)
            assert state is not None
            cell = state.code_cells[0]
            assert cell.execution_count == 3, f"third click must be 3, got {cell.execution_count}"
            assert "[In [3]]" in _gutter_line_for_cell(doc, cell.index)
            assert doc.getBookmarks().hasByName(bm_name)
            out3 = _output_text_for_cell(doc, cell)
            body3 = doc.getText().getString() or ""
            assert out3.count(needle) == 1
            assert body3.count(needle) == 1, f"third click dumped extra copies at end: {body3[-400:]!r}"
            _assert_controls_present(doc, cell)

    import plugin.scripting.session_manager as sm

    with (
        patch.object(sm, "_msgbox", lambda *args, **kwargs: None),
        patch.object(sm, "reset_python_session", return_value={"status": "ok"}),
    ):
        sm.reset_workbook_python_session(ctx, doc)
    state = load_registry(doc)
    assert state is not None
    assert state.next_execution_count == 1, (
        f"Restart Kernel must reset In count to 1, got {state.next_execution_count}"
    )

