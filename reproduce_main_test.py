import sys
import threading
from unittest.mock import MagicMock, patch

# Mock some basics
sys.modules["uno"] = MagicMock()
sys.modules["unohelper"] = MagicMock()
class MockBase: pass
class Mock1(MockBase): pass
class Mock2(MockBase): pass
class Mock3(MockBase): pass
class Mock4(MockBase): pass
class Mock5(MockBase): pass

sys.modules["unohelper"].Base = Mock1
sys.modules["com"] = MagicMock()
sys.modules["com.sun"] = MagicMock()
sys.modules["com.sun.star"] = MagicMock()
sys.modules["com.sun.star.task"] = MagicMock()
sys.modules["com.sun.star.task"].XJobExecutor = Mock2
sys.modules["com.sun.star.task"].XJob = Mock3
sys.modules["com.sun.star.frame"] = MagicMock()
sys.modules["com.sun.star.frame"].XDispatch = Mock4
sys.modules["com.sun.star.frame"].XDispatchProvider = Mock5
sys.modules["com.sun.star.lang"] = MagicMock()
sys.modules["com.sun.star.lang"].XInitialization = MagicMock
sys.modules["com.sun.star.lang"].XServiceInfo = MagicMock

import plugin.main as main_mod

def fake_run_module_suite(ctx, module, name, doc_model=None):
    print("fake_run_module_suite called!")
    return (0, 0, [])

fake_ctx = MagicMock()
with (
    patch("plugin.framework.uno_context.get_ctx", return_value=fake_ctx),
    patch.object(main_mod, "get_active_document", return_value=None),
    patch("plugin.chatbot.dialogs.msgbox") as mock_msg,
    patch("plugin.testing_runner.run_module_suite", side_effect=fake_run_module_suite) as mock_run,
):
    print("Calling _run_test_suite...")
    main_mod._run_test_suite(MagicMock(), lambda _m: True, "writer.format_tests")
    print(f"Mock called? {mock_run.called}")
    if not mock_run.called:
        if mock_msg.called:
             print(f"Msgbox called with: {mock_msg.call_args}")
