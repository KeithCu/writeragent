# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO test for Calc shared-kernel session reset (Issue #411 / Packet C2.2)."""

from __future__ import annotations

import math
import struct
from unittest.mock import patch

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _assert_undefined_name_error(val: object) -> None:
    # Undefined =PY() names raise InterpreterError in the sandbox, then
    # addin_result_for_worker_status maps that to #VALUE! (519). The cell
    # value is a quiet-NaN double; the "not defined" sentence is the log line.
    assert isinstance(val, float)
    assert not math.isfinite(val)
    raw = struct.unpack("Q", struct.pack("d", val))[0]
    assert raw == (0x7FF8000000000000 | 519)


@native_test
@with_native_doc("calc")
def test_shared_kernel_reset_session_uno(ctx, doc):
    from plugin.calc.python.addin import PythonFunction
    from plugin.framework.config import save
    import plugin.scripting.session_manager as sm
    from plugin.scripting.document_scripts import set_calc_init_script

    try:
        save({"scripting.python_session_mode": "shared"})

        # 1. Standard add-in instantiation; the caller argument carries the document
        func_default = PythonFunction(ctx)
        res_default1 = func_default.py(doc, "shared_val = 100")
        assert res_default1 == 100.0
        res_default2 = func_default.py(doc, "shared_val")
        assert res_default2 == 100.0

        func = PythonFunction(ctx)

        sid = sm.workbook_session_id(doc=doc)
        assert sid is not None
        assert sid.startswith("calc:")

        # 2. C2.2.1: Shared names persist prior to reset
        res1 = func.py(doc, "x = 42")
        assert res1 == 42.0
        res2 = func.py(doc, "x")
        assert res2 == 42.0

        # 3. C2.2.2: Leftover result persists prior to reset
        res_r1 = func.py(doc, "result = 20")
        assert res_r1 == 20.0
        res_r2 = func.py(doc, "result")
        assert res_r2 == 20.0

        # Attach an init script with custom helper function (C2.2.3)
        set_calc_init_script(doc, "def double(x):\n    return x * 2\n")
        res_init1 = func.py(doc, "result = double(3)")
        assert res_init1 == 6.0

        # 4. Reset Python Session (suppress UI modal msgbox)
        with patch.object(sm, "_msgbox", lambda *args, **kwargs: None):
            sm.reset_workbook_python_session(ctx, doc=doc)

        # 5. C2.2.1: Verify shared variable x was dropped
        res3 = func.py(doc, "x")
        _assert_undefined_name_error(res3)

        # 6. C2.2.2: Verify leftover result was cleared
        res_r3 = func.py(doc, "result")
        assert res_r3 != 20.0
        _assert_undefined_name_error(res_r3)

        # 7. C2.2.3: Verify init helper function double(x) is re-applied and functional after Reset (both default and explicit ctor)
        res_init2 = func.py(doc, "result = double(4)")
        assert res_init2 == 8.0
        res_init_default = func_default.py(doc, "result = double(5)")
        assert res_init_default == 10.0
    finally:
        save({"scripting.python_session_mode": "isolated"})


@native_test
@with_native_doc("calc")
def test_shared_kernel_live_cells_reset_uno(ctx, doc):


    """Test live Calc cells using =PY() formula recalculation in shared mode and after reset."""
    from plugin.framework.config import save
    import plugin.scripting.session_manager as sm
    from plugin.scripting.document_scripts import set_calc_init_script
    from plugin.calc.python.function import clear_python_addin_cache
    from plugin.scripting.venv_worker import shutdown_venv_workers_for_tests

    shutdown_venv_workers_for_tests()
    clear_python_addin_cache()




    try:
        save({"scripting.python_session_mode": "shared"})

        # Record active Calc session id on the main thread for off-main formula lookups
        sid = sm.calc_workbook_base_session_id(doc)
        assert sid is not None

        from plugin.calc.python.addin import PythonFunction

        func = PythonFunction(ctx)
        res1 = func.py(doc, "x_live = 42")
        assert res1 == 42.0
        res2 = func.py(doc, "result = x_live")
        assert res2 == 42.0

        # Attach init script (C2.2.3)
        set_calc_init_script(doc, "def double(x):\n    return x * 2\n")
        func = PythonFunction(ctx)
        res_c1 = func.py(doc, "result = double(3)")
        assert res_c1 == 6.0

        # 1. Reset Python session in test runner
        with patch.object(sm, "_msgbox", lambda *args, **kwargs: None):
            sm.reset_workbook_python_session(ctx, doc=doc)

        # 2. Verify shared variable x is dropped and helper double(7) works via PythonFunction(ctx) (default ctor)
        from plugin.calc.python.addin import PythonFunction

        func = PythonFunction(ctx)
        res_x_post = func.py(doc, "x")
        _assert_undefined_name_error(res_x_post)

        res_double_post = func.py(doc, "result = double(7)")
        assert res_double_post == 14.0

        # 3. Verify init script helper works post-reset
        res_d1 = func.py(doc, "result = double(7)")
        assert res_d1 == 14.0

    finally:
        save({"scripting.python_session_mode": "isolated"})


@native_test
@with_native_doc("calc")
def test_shared_kernel_removed_init_drops_bindings_uno(ctx, doc):
    from plugin.calc.python.addin import PythonFunction
    from plugin.doc.udprops import remove_document_property, set_document_property
    from plugin.framework.config import save
    from plugin.scripting.document_scripts import DOCUMENT_SCRIPTS_UDPROP, set_calc_init_script

    try:
        save({"scripting.python_session_mode": "shared"})
        func = PythonFunction(ctx)

        # 1. Never-initialised kernel keeps variables across calls.
        assert func.py(doc, "keep = 5") == 5.0
        assert func.py(doc, "result = keep") == 5.0

        # 2. Seed INIT.
        set_calc_init_script(doc, "HELPER = 7")
        assert func.py(doc, "result = HELPER") == 7.0

        # 3. Clear INIT -> next =PY() drops old init names.
        set_calc_init_script(doc, "")
        _assert_undefined_name_error(func.py(doc, "result = HELPER"))

        # 4. Drop happens once, not on every call.
        assert func.py(doc, "nv = 3") == 3.0
        assert func.py(doc, "result = nv") == 3.0

        # 5. Re-seed then whitespace-only INIT -> dropped again.
        set_calc_init_script(doc, "HELPER = 8")
        assert func.py(doc, "result = HELPER") == 8.0
        set_calc_init_script(doc, "  \n ")
        _assert_undefined_name_error(func.py(doc, "result = HELPER"))

        # 6. Re-seed then corrupt property -> unreadable map keeps kernel.
        set_calc_init_script(doc, "HELPER = 9")
        assert func.py(doc, "result = HELPER") == 9.0
        assert set_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, "{bad json")
        assert func.py(doc, "result = HELPER") == 9.0

        # 7. Remove property altogether -> dropped.
        assert remove_document_property(doc, DOCUMENT_SCRIPTS_UDPROP)
        _assert_undefined_name_error(func.py(doc, "result = HELPER"))
    finally:
        save({"scripting.python_session_mode": "isolated"})
