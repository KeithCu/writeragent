# WriterAgent - Native UNO Test for Thread Guard Method Proxying
from __future__ import annotations

import os
import threading

from plugin.framework import thread_guard
from plugin.framework.thread_guard import _UnoThreadGuardProxy, guard_uno
from plugin.testing_runner import native_test


@native_test
def test_guard_follows_pyuno_method_call(ctx):
    saved_guard = thread_guard.GUARD_ON
    thread_guard.GUARD_ON = True
    try:
        wrapped = guard_uno(ctx)
        method = wrapped.getServiceManager
        assert isinstance(method, _UnoThreadGuardProxy)
        assert type(method._target).__name__ == "PyUNO_callable"

        smgr = method()
        assert isinstance(smgr, _UnoThreadGuardProxy)

        # Only after those asserts, start a threading.Thread that reads smgr.getImplementationName.
        # assert_main_thread runs before the raw getattr. Expect RuntimeError containing UNO thread violation.
        err = []

        def worker():
            saved_testing = os.environ.get("WRITERAGENT_TESTING")
            os.environ["WRITERAGENT_TESTING"] = "1"
            try:
                _ = smgr.getImplementationName
            except RuntimeError as exc:
                err.append(exc)
            finally:
                if saved_testing is None:
                    os.environ.pop("WRITERAGENT_TESTING", None)
                else:
                    os.environ["WRITERAGENT_TESTING"] = saved_testing

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert len(err) == 1
        assert "UNO thread violation" in str(err[0])
    finally:
        thread_guard.GUARD_ON = saved_guard
