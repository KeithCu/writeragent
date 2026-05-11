# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc = None
_test_ctx = None

@setup
def setup_calc_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    _test_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden_prop,))

@teardown
def teardown_calc_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None

def _execute_calc_tool(name, args):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool import ToolContext
    # Pass suite bootstrap ctx (same as setup_calc_tests); None makes
    # get_desktop() use uno.getComponentContext() and can segfault under
    # python -m plugin.testing_runner.
    tctx = ToolContext(_test_doc, _test_ctx, "calc", get_services(), "test")
    try:
        res = get_tools().execute(name, tctx, **args)
    except (KeyError, ValueError) as e:
        res = {"status": "error", "error": str(e)}
    return res


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
def test_insert_cell_html():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool(
        "insert_cell_html",
        {
            "cell_address": "Z99",
            "html": "Plain <b>BoldBit</b> tail",
        },
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
