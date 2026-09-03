# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _execute_calc_tool(doc, ctx, name, args):
    return TestingFactory.execute_tool(doc, ctx, name, args, doc_type="calc")


def _diagnose_insert_cell_html_bold(cell) -> str:
    """Only used when the bold assertion fails: dump CharWeight / snippet per portion."""
    lines: list[str] = []
    i = 0
    for portion in _iter_cell_text_portions_for_test(cell):
        i += 1
        s = "?"
        wv = tpt = None
        try:
            s = portion.getString()
        except Exception as ex:
            s = f"<getString: {ex!r}>"
        try:
            wv = portion.getPropertyValue("CharWeight")
        except Exception as ex:
            wv = f"<CharWeight: {ex!r}>"
        try:
            tpt = portion.getPropertyValue("TextPortionType")
        except Exception:
            pass
        lines.append(
            f"  portion[{i}] CharWeight={wv!r} TextPortionType={tpt!r} s={s!r} "
            f"is_bold={_is_bold_char_weight(wv) if not isinstance(wv, str) else 'n/a'}"
        )
    if not lines:
        return "  (no portions enumerated)"
    return "\n".join(lines)


def _iter_cell_text_portions_for_test(cell):
    """Calc cells may not advertise ``Paragraph``; mirror document.get_string_without_… logic."""
    text = cell.getText()
    top = text.createEnumeration()
    while top.hasMoreElements():
        block = top.nextElement()
        try:
            inner = block.createEnumeration()
        except Exception:
            yield block
            continue
        any_inner = False
        while inner.hasMoreElements():
            any_inner = True
            yield inner.nextElement()
        if not any_inner:
            yield block


def _is_bold_char_weight(wv) -> bool:
    """UNO may use float/enum; BOLD is 150, NORMAL 100 in awt.FontWeight."""
    if wv is None:
        return False
    try:
        from com.sun.star.awt import FontWeight

        if wv == FontWeight.BOLD:
            return True
    except Exception:
        pass
    try:
        return float(wv) >= 135.0
    except (TypeError, ValueError):
        return False


@native_test
@with_native_doc("calc")
def test_insert_cell_html(ctx, doc):
    from plugin.testing_runner import _progress, _soffice_pids
    import os

    active_sheet = doc.getCurrentController().getActiveSheet()
    _progress(
        "insert_cell_html: execute start python_pid=%s soffice=%s"
        % (os.getpid(), _soffice_pids())
    )
    res = _execute_calc_tool(
        doc,
        ctx,
        "insert_cell_html",
        {
            "cell": "Z99",
            "html": "Plain <b>BoldBit</b> tail",
        },
    )
    _progress(
        "insert_cell_html: execute done status=%s python_pid=%s soffice=%s"
        % (res.get("status"), os.getpid(), _soffice_pids())
    )
    assert res.get("status") == "ok", f"insert_cell_html failed: {res}"
    cell = active_sheet.getCellByPosition(25, 98)
    s = cell.getString()
    assert "BoldBit" in s and "Plain" in s and "tail" in s, f"unexpected cell string: {s!r}"

    has_bold = False
    for portion in _iter_cell_text_portions_for_test(cell):
        try:
            wv = portion.getPropertyValue("CharWeight")
        except Exception:
            continue
        try:
            ptxt = portion.getString()
        except Exception:
            ptxt = ""
        if _is_bold_char_weight(wv) and "BoldBit" in ptxt:
            has_bold = True
            break
    assert has_bold, (
        "expected a bold text portion containing BoldBit; diagnosis:\n"
        + _diagnose_insert_cell_html_bold(cell)
    )
