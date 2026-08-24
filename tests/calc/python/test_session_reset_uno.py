# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO test for Calc shared-kernel session reset (Issue #411 / Packet C2.2)."""

from __future__ import annotations

from unittest.mock import patch

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_shared_kernel_reset_session_uno(ctx, doc):
    from plugin.calc.python.addin import PythonFunction
    from plugin.framework.config import set_config
    import plugin.scripting.session_manager as sm
    from plugin.scripting.document_scripts import set_calc_init_script

    try:
        set_config("scripting.python_session_mode", "shared")

        # 1. Verify standard add-in instantiation (no doc= passed, matching Calc formula engine)
        func_default = PythonFunction(ctx)
        res_default1 = func_default.py("shared_val = 100")
        assert res_default1 == 100.0
        res_default2 = func_default.py("shared_val")
        assert res_default2 == 100.0

        # Pass doc into PythonFunction to verify explicit doc forwarding
        func = PythonFunction(ctx, doc=doc)

        sid = sm.workbook_session_id(ctx, doc=doc)
        assert sid is not None
        assert sid.startswith("calc:")

        # 2. C2.2.1: Shared names persist prior to reset
        res1 = func.py("x = 42")
        assert res1 == 42.0
        res2 = func.py("x")
        assert res2 == 42.0

        # 3. C2.2.2: Leftover result persists prior to reset
        res_r1 = func.py("result = 20")
        assert res_r1 == 20.0
        res_r2 = func.py("result")
        assert res_r2 == 20.0

        # Attach an init script with custom helper function (C2.2.3)
        set_calc_init_script(doc, "def double(x):\n    return x * 2\n")
        res_init1 = func.py("result = double(3)")
        assert res_init1 == 6.0

        # 4. Reset Python Session (suppress UI modal msgbox)
        with patch.object(sm, "_msgbox", lambda *args, **kwargs: None):
            sm.reset_workbook_python_session(ctx, doc=doc)

        # 5. C2.2.1: Verify shared variable x was dropped
        res3 = func.py("x")
        assert "not defined" in str(res3) or "Error:" in str(res3)

        # 6. C2.2.2: Verify leftover result was cleared
        res_r3 = func.py("result")
        assert res_r3 != 20.0
        assert "not defined" in str(res_r3) or "Error:" in str(res_r3)

        # 7. C2.2.3: Verify init helper function double(x) is re-applied and functional after Reset (both default and explicit ctor)
        res_init2 = func.py("result = double(4)")
        assert res_init2 == 8.0
        res_init_default = func_default.py("result = double(5)")
        assert res_init_default == 10.0
    finally:
        set_config("scripting.python_session_mode", "isolated")


@native_test
@with_native_doc("calc")
def test_shared_kernel_live_cells_reset_uno(ctx, doc):


    """Test live Calc cells using =PY() formula recalculation in shared mode and after reset."""
    from plugin.framework.config import set_config
    import plugin.scripting.session_manager as sm
    from plugin.scripting.document_scripts import set_calc_init_script
    from plugin.calc.python.function import clear_python_addin_cache
    from plugin.scripting.venv_worker import PythonWorkerManager

    PythonWorkerManager.shutdown_all()
    clear_python_addin_cache()




    try:
        set_config("scripting.python_session_mode", "shared")

        # Record active Calc session id on the main thread for off-main formula lookups
        sid = sm.calc_workbook_base_session_id(doc)
        assert sid is not None

        from plugin.calc.python.addin import PythonFunction

        func = PythonFunction(ctx, doc=doc)
        res1 = func.py("x_live = 42")
        assert res1 == 42.0
        res2 = func.py("result = x_live")
        assert res2 == 42.0

        # Attach init script (C2.2.3)
        set_calc_init_script(doc, "def double(x):\n    return x * 2\n")
        func = PythonFunction(ctx, doc=doc)
        res_c1 = func.py("result = double(3)")
        assert res_c1 == 6.0





        # 1. Reset Python session in test runner
        with patch.object(sm, "_msgbox", lambda *args, **kwargs: None):
            sm.reset_workbook_python_session(ctx, doc=doc)

        # 2. Verify shared variable x is dropped and helper double(7) works via PythonFunction(ctx) (default ctor)
        from plugin.calc.python.addin import PythonFunction

        func = PythonFunction(ctx)
        res_x_post = func.py("x")
        assert "not defined" in str(res_x_post) or "Error:" in str(res_x_post)

        res_double_post = func.py("result = double(7)")
        assert res_double_post == 14.0

        # 3. Verify init script helper works post-reset
        res_d1 = func.py("result = double(7)")
        assert res_d1 == 14.0

    finally:
        set_config("scripting.python_session_mode", "isolated")
