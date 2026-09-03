# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.testing_runner import _progress, native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _execute_calc_tool(doc, ctx, name, args):
    return TestingFactory.execute_tool(doc, ctx, name, args, doc_type="calc")


def _diagnose_insert_cell_html_bold(cell) -> str:
    """Only used when the bold assertion fails: dump CharWeight / snippet per portion."""
    # GHA 33703959362: execute-done then silence. If the bold assert fails and
    # diagnosis hangs, these lines name createEnumeration vs getString.
    _progress("insert_cell_html: diagnose start")
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
    _progress("insert_cell_html: diagnose done portions=%s" % i)
    if not lines:
        return "  (no portions enumerated)"
    return "\n".join(lines)


def _iter_cell_text_portions_for_test(cell):
    """Calc cells may not advertise ``Paragraph``; mirror document.get_string_without_… logic."""
    # GHA 33703959362 hung after execute-done with no TEST end. Enumeration
    # (createEnumeration / hasMoreElements / nextElement) is one hang candidate.
    _progress("insert_cell_html: portions createEnumeration start")
    text = cell.getText()
    top = text.createEnumeration()
    _progress("insert_cell_html: portions createEnumeration done")
    block_i = 0
    yielded = 0
    while True:
        _progress("insert_cell_html: portions hasMoreElements block=%s" % block_i)
        if not top.hasMoreElements():
            _progress(
                "insert_cell_html: portions hasMoreElements=False block=%s yielded=%s"
                % (block_i, yielded)
            )
            break
        _progress("insert_cell_html: portions nextElement start block=%s" % block_i)
        block = top.nextElement()
        block_i += 1
        _progress("insert_cell_html: portions nextElement done block=%s" % block_i)
        try:
            _progress(
                "insert_cell_html: portions inner createEnumeration start block=%s" % block_i
            )
            inner = block.createEnumeration()
            _progress(
                "insert_cell_html: portions inner createEnumeration done block=%s" % block_i
            )
        except Exception:
            _progress(
                "insert_cell_html: portions inner createEnumeration failed; yield block=%s"
                % block_i
            )
            yielded += 1
            yield block
            continue
        any_inner = False
        inner_i = 0
        while True:
            _progress(
                "insert_cell_html: portions inner hasMoreElements block=%s inner=%s"
                % (block_i, inner_i)
            )
            if not inner.hasMoreElements():
                break
            any_inner = True
            _progress(
                "insert_cell_html: portions inner nextElement start block=%s inner=%s"
                % (block_i, inner_i)
            )
            portion = inner.nextElement()
            _progress(
                "insert_cell_html: portions inner nextElement done block=%s inner=%s"
                % (block_i, inner_i)
            )
            inner_i += 1
            yielded += 1
            yield portion
        if not any_inner:
            yielded += 1
            yield block
    _progress("insert_cell_html: portions enum exit yielded=%s" % yielded)


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
    from plugin.testing_runner import _soffice_pids
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
    # GHA 33703959362: last line was execute-done; no TEST end. Name each
    # post-execute UNO/assert so the next Windows timeout is not silent.
    _progress("insert_cell_html: status assert start")
    assert res.get("status") == "ok", f"insert_cell_html failed: {res}"
    _progress("insert_cell_html: status assert done")
    _progress("insert_cell_html: getCellByPosition start")
    cell = active_sheet.getCellByPosition(25, 98)
    _progress("insert_cell_html: getCellByPosition done")
    _progress("insert_cell_html: getString start")
    s = cell.getString()
    _progress("insert_cell_html: getString done s=%r" % (s[:80] if isinstance(s, str) else s,))
    assert "BoldBit" in s and "Plain" in s and "tail" in s, f"unexpected cell string: {s!r}"
    _progress("insert_cell_html: string assert done")

    has_bold = False
    _progress("insert_cell_html: portion loop start")
    n = 0
    for portion in _iter_cell_text_portions_for_test(cell):
        n += 1
        _progress("insert_cell_html: portion[%s] CharWeight start" % n)
        try:
            wv = portion.getPropertyValue("CharWeight")
        except Exception as ex:
            _progress("insert_cell_html: portion[%s] CharWeight failed: %r" % (n, ex))
            continue
        _progress("insert_cell_html: portion[%s] CharWeight done wv=%r" % (n, wv))
        _progress("insert_cell_html: portion[%s] getString start" % n)
        try:
            ptxt = portion.getString()
        except Exception as ex:
            _progress("insert_cell_html: portion[%s] getString failed: %r" % (n, ex))
            ptxt = ""
        _progress("insert_cell_html: portion[%s] getString done ptxt=%r" % (n, ptxt))
        if _is_bold_char_weight(wv) and "BoldBit" in ptxt:
            has_bold = True
            _progress("insert_cell_html: portion[%s] bold match" % n)
            break
    _progress("insert_cell_html: portion loop done count=%s has_bold=%s" % (n, has_bold))
    _progress("insert_cell_html: bold assert start")
    assert has_bold, (
        "expected a bold text portion containing BoldBit; diagnosis:\n"
        + _diagnose_insert_cell_html_bold(cell)
    )
    _progress("insert_cell_html: bold assert done")
    _progress("insert_cell_html: assertions done")
    # GHA 33763078357 hung in teardown getDocumentProperties after clearContents.
    # Probe the same SfxObjectShell call *before* wipe so the next Windows
    # timeout says whether the model is already wedged after insert+close.
    _progress("insert_cell_html: getDocumentProperties probe start")
    props = doc.getDocumentProperties()
    _progress(
        "insert_cell_html: getDocumentProperties probe done props=%s"
        % (props is not None,)
    )
