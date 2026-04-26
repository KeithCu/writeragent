"""Tests for AI grammar proofreader wait/pump helpers."""

from __future__ import annotations

import sys
import threading
import time
import types


def _install_ai_grammar_import_mocks() -> None:
    uno_mod = sys.modules.setdefault("uno", types.ModuleType("uno"))
    setattr(uno_mod, "getConstantByName", lambda _name: 4)

    unohelper_mod = sys.modules.setdefault("unohelper", types.ModuleType("unohelper"))

    class MockBase:
        pass

    class MockImplementationHelper:
        def addImplementation(self, *_args, **_kwargs) -> None:
            return None

    setattr(unohelper_mod, "Base", MockBase)
    setattr(unohelper_mod, "ImplementationHelper", MockImplementationHelper)

    for name in (
        "com",
        "com.sun",
        "com.sun.star",
        "com.sun.star.lang",
        "com.sun.star.linguistic2",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))

    class MockLocale:
        def __init__(self, Language: str, Country: str, Variant: str):
            self.Language = Language
            self.Country = Country
            self.Variant = Variant

    class MockXServiceDisplayName:
        pass

    class MockXServiceInfo:
        pass

    class MockXServiceName:
        pass

    class MockXProofreader:
        pass

    class MockXSupportedLocales:
        pass

    lang_mod = sys.modules["com.sun.star.lang"]
    setattr(lang_mod, "Locale", MockLocale)
    setattr(lang_mod, "XServiceDisplayName", MockXServiceDisplayName)
    setattr(lang_mod, "XServiceInfo", MockXServiceInfo)
    setattr(lang_mod, "XServiceName", MockXServiceName)

    linguistic_mod = sys.modules["com.sun.star.linguistic2"]
    setattr(linguistic_mod, "XProofreader", MockXProofreader)
    setattr(linguistic_mod, "XSupportedLocales", MockXSupportedLocales)


_install_ai_grammar_import_mocks()


class _Toolkit:
    def __init__(self) -> None:
        self.pump_count = 0

    def processEventsToIdle(self) -> None:
        self.pump_count += 1


class _ServiceManager:
    def __init__(self, toolkit: _Toolkit) -> None:
        self.toolkit = toolkit

    def createInstanceWithContext(self, _name: str, _ctx: object) -> _Toolkit:
        return self.toolkit


class _Ctx:
    def __init__(self, toolkit: _Toolkit) -> None:
        self._smgr = _ServiceManager(toolkit)

    def getServiceManager(self) -> _ServiceManager:
        return self._smgr


def test_wait_for_inflight_job_pumps_until_done() -> None:
    from plugin.modules.writer.ai_grammar_proofreader import (
        _InflightGrammarJob,
        _wait_for_inflight_job,
    )

    toolkit = _Toolkit()
    job = _InflightGrammarJob()

    def finish_later() -> None:
        time.sleep(0.15)
        job.done.set()

    thread = threading.Thread(target=finish_later, daemon=True)
    thread.start()

    assert _wait_for_inflight_job(_Ctx(toolkit), job, 1000) is True
    assert toolkit.pump_count >= 1


def test_wait_for_inflight_job_timeout_returns_false() -> None:
    from plugin.modules.writer.ai_grammar_proofreader import (
        _InflightGrammarJob,
        _wait_for_inflight_job,
    )

    toolkit = _Toolkit()
    job = _InflightGrammarJob()

    assert _wait_for_inflight_job(_Ctx(toolkit), job, 50) is False
    assert toolkit.pump_count >= 1
