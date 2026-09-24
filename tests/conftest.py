import os

# Layer A thread guard defaults on in non-release bundles; keep headless pytest stable.
os.environ.setdefault("WRITERAGENT_UNO_THREAD_GUARD", "0")

import sys
import time
import types
from unittest.mock import MagicMock, patch

import pytest



def pytest_collection_modifyitems(config, items):
    """Drop leftover @native_test items so they are not counted as skipped.

    The real pytest/UNO split is ``--ignore-glob=*_uno.py`` (``make pytest`` /
    pyproject addopts). This hook still catches mixed modules such as
    ``test_uno_context.py`` that keep a few ``@native_test`` functions beside
    headless unit tests.
    """
    # This ensures that 'skipped' in pytest output only refers to actually disabled tests.
    def is_native(item):
        # 1. Check for the @native_test decorator attribute on the function
        func = getattr(item, "obj", None)
        if func and getattr(func, "_is_test", False):
            return True
            
        # 2. Check for pytest.mark.skip(reason="...native runner...") 
        # This catches both module-level and function-level markers
        for marker in item.iter_markers(name="skip"):
            reason = str(marker.kwargs.get("reason", ""))
            if "native runner" in reason or "Run by native runner" in reason:
                return True
        return False

    items[:] = [item for item in items if not is_native(item)]


# Create a mock for uno to prevent ModuleNotFoundError in headless tests
sys.modules["uno"] = MagicMock()


class MockUnohelperBase:
    pass


_uh = types.ModuleType("unohelper")
_uh.Base = MockUnohelperBase
_uh.ImplementationHelper = MagicMock
sys.modules["unohelper"] = _uh

def _create_mock_module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


class MockBase:
    pass

# Unique mock classes for UNO interfaces to avoid TypeError during multiple inheritance
class MockXProofreader: pass
class MockXLinguServiceEventBroadcaster: pass
class MockXSupportedLocales: pass
class MockXServiceDisplayName: pass
class MockXServiceInfo: pass
class MockXServiceName: pass
class MockPropertyValue:
    def __init__(self, Name=None, Value=None):
        self.Name = Name
        self.Value = Value


@pytest.fixture(autouse=True)
def _disable_dev_llm_prefix_for_deterministic_http_tests():
    """Real dev bundles prepend a system prompt; keep unit test request JSON stable."""
    with patch(
        "plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix",
        return_value=False,
    ):
        yield


@pytest.fixture(autouse=True)
def _reset_calc_session_manager_state():
    """Ensure Calc session state does not leak between unit tests."""
    from plugin.scripting.session_manager import clear_active_calc_session

    clear_active_calc_session()
    yield
    clear_active_calc_session()


@pytest.fixture(autouse=True)
def _ensure_writeragent_logger_propagates():
    """Ensure writeragent log records propagate to root logger for caplog fixtures."""
    import logging

    wa_logger = logging.getLogger("writeragent")
    old_propagate = wa_logger.propagate
    wa_logger.propagate = True
    yield
    wa_logger.propagate = old_propagate



com = _create_mock_module("com")
sun = _create_mock_module("com.sun")
star = _create_mock_module("com.sun.star")
sys.modules["com.sun.star"].__path__ = []  # Make it act as a package

awt = _create_mock_module("com.sun.star.awt")


class MockSize:
    def __init__(self, width=0, height=0):
        self.Width = width
        self.Height = height


class MockPoint:
    def __init__(self, x=0, y=0):
        self.X = x
        self.Y = y


class MockRectangle:
    def __init__(self, x=0, y=0, width=0, height=0):
        self.X = x
        self.Y = y
        self.Width = width
        self.Height = height


setattr(awt, "Point", MockPoint)
setattr(awt, "Rectangle", MockRectangle)
setattr(awt, "Size", MockSize)
setattr(awt, "FontWeight", MockBase)
setattr(awt, "FontSlant", MockBase)

text = _create_mock_module("com.sun.star.text")
sys.modules["com.sun.star.text"].__path__ = []
sys.modules["com.sun.star.text.TextContentAnchorType"] = _create_mock_module("com.sun.star.text.TextContentAnchorType")
setattr(sys.modules["com.sun.star.text.TextContentAnchorType"], "AS_CHARACTER", MockBase)
setattr(sys.modules["com.sun.star.text.TextContentAnchorType"], "AT_FRAME", MockBase)
setattr(sys.modules["com.sun.star.text.TextContentAnchorType"], "AT_PAGE", MockBase)
setattr(sys.modules["com.sun.star.text.TextContentAnchorType"], "AT_PARAGRAPH", MockBase)
setattr(sys.modules["com.sun.star.text.TextContentAnchorType"], "AT_CHARACTER", MockBase)

linguistic = _create_mock_module("com.sun.star.linguistic2")
setattr(linguistic, "XProofreader", MockXProofreader)
setattr(linguistic, "XSupportedLocales", MockXSupportedLocales)
setattr(linguistic, "XLinguServiceEventBroadcaster", MockXLinguServiceEventBroadcaster)

beans = _create_mock_module("com.sun.star.beans")
setattr(beans, "PropertyValue", MockPropertyValue)

sheet = _create_mock_module("com.sun.star.sheet")
setattr(beans, "PropertyValue", MockPropertyValue) # Repeat for safety in case of earlier failure

sheet = _create_mock_module("com.sun.star.sheet")
setattr(sheet, "ConditionOperator", MockBase)
setattr(sheet, "ConditionOperator2", MockBase)

table = _create_mock_module("com.sun.star.table")

lang = _create_mock_module("com.sun.star.lang")

util = _create_mock_module("com.sun.star.util")
setattr(util, "XModifyListener", MockBase)  # review_toolbar._ReviewModifyListener subclasses it


class MockXEventListener:
    pass


setattr(lang, "XEventListener", MockXEventListener)
setattr(lang, "XServiceDisplayName", MockXServiceDisplayName)
setattr(lang, "XServiceInfo", MockXServiceInfo)
setattr(lang, "XServiceName", MockXServiceName)
setattr(lang, "XInitialization", MockBase)
setattr(lang, "DisposedException", Exception)
setattr(lang, "IllegalArgumentException", Exception)

uno_mod = _create_mock_module("com.sun.star.uno")
setattr(uno_mod, "Exception", Exception)
setattr(uno_mod, "RuntimeException", Exception)

container = _create_mock_module("com.sun.star.container")
setattr(container, "NoSuchElementException", Exception)

class MockXSidebarPanel: pass
class MockXToolPanel: pass
class MockXUIElement: pass
class MockXUIElementFactory: pass

ui_mod = _create_mock_module("com.sun.star.ui")
setattr(ui_mod, "XSidebarPanel", MockXSidebarPanel)
setattr(ui_mod, "XToolPanel", MockXToolPanel)
setattr(ui_mod, "XUIElement", MockXUIElement)
setattr(ui_mod, "XUIElementFactory", MockXUIElementFactory)


class MockXActionListener:
    pass


class MockXItemListener:
    pass


class MockXKeyListener:
    pass


class MockXTextListener:
    pass


class MockXWindowListener:
    pass


class MockXTopWindowListener:
    pass


setattr(awt, "XActionListener", MockXActionListener)
setattr(awt, "XItemListener", MockXItemListener)
setattr(awt, "XKeyListener", MockXKeyListener)
setattr(awt, "XTextListener", MockXTextListener)
setattr(awt, "XWindowListener", MockXWindowListener)
setattr(awt, "XTopWindowListener", MockXTopWindowListener)
setattr(awt, "WindowDescriptor", MockBase)

awt_window_class = _create_mock_module("com.sun.star.awt.WindowClass")
setattr(awt_window_class, "CONTAINER", 0)
setattr(awt_window_class, "TOP", 1)

class MockXJobExecutor: pass
class MockXJob: pass
class MockXDispatch: pass
class MockXDispatchProvider: pass

task = _create_mock_module("com.sun.star.task")
setattr(task, "XJobExecutor", MockXJobExecutor)
setattr(task, "XJob", MockXJob)

frame = _create_mock_module("com.sun.star.frame")
setattr(frame, "DispatchDescriptor", MockBase)
setattr(frame, "XDispatch", MockXDispatch)
setattr(frame, "XDispatchProvider", MockXDispatchProvider)

# Shells and attributes that setup_uno_mocks() used to install per test module.
# They live here so a module-level setup_uno_mocks() call is a no-op under pytest.
_create_mock_module("com.sun.star.document")
_create_mock_module("com.sun.star.style")
_create_mock_module("com.sun.star.style.BreakType")
_create_mock_module("com.sun.star.ui.UIElementType")
_create_mock_module("com.sun.star.datatransfer")
clipboard = _create_mock_module("com.sun.star.datatransfer.clipboard")


class MockDate:
    Year = 2024
    Month = 1
    Day = 1


class MockXClipboardListener:
    pass


class MockXCallback:
    pass


setattr(util, "Date", MockDate)
setattr(clipboard, "XClipboardListener", MockXClipboardListener)
setattr(awt, "XCallback", MockXCallback)
# setup_uno_mocks also publishes unohelper.Base as its own sys.modules entry.
sys.modules["unohelper.Base"] = MockUnohelperBase

# Some test modules replace these entries at import (for example unohelper = MagicMock()).
# The next module then subclasses MagicMock and collection dies with a metaclass conflict.
# Snapshot the session shells and put them back before each test module imports, so a
# per-file setup_uno_mocks() call is not required to undo that.
_PYTEST_UNO_SHELLS = {
    name: mod
    for name, mod in list(sys.modules.items())
    if name in ("uno", "unohelper")
    or name.startswith("unohelper.")
    or name == "com"
    or name.startswith("com.")
}
# Attribute writes on the shared module object (not a replacement of the module)
# survive putting the same object back into sys.modules. Re-apply the originals.
_PYTEST_UNO_ATTRS: list[tuple[types.ModuleType, str, object]] = []
for _mod in _PYTEST_UNO_SHELLS.values():
    if not isinstance(_mod, types.ModuleType):
        continue
    for _attr, _val in list(vars(_mod).items()):
        if _attr.startswith("_"):
            continue
        _PYTEST_UNO_ATTRS.append((_mod, _attr, _val))


def _restore_pytest_uno_shells() -> None:
    for name, mod in _PYTEST_UNO_SHELLS.items():
        sys.modules[name] = mod
    for mod, attr, val in _PYTEST_UNO_ATTRS:
        setattr(mod, attr, val)


def pytest_collectstart(collector):
    """Restore session UNO shells immediately before a test module is imported.

    pytest_collect_file runs when the collector is created, which can be long
    before import. collectstart on the module runs inside collection, just
    before Module._getobj imports the file. Test modules that assign
    sys.modules['unohelper'] = MagicMock() would otherwise leak into the next
    file and break unohelper.Base multiple inheritance.
    """
    path = getattr(collector, "path", None)
    if path is not None and str(path).endswith(".py"):
        _restore_pytest_uno_shells()


@pytest.fixture(autouse=True)
def _setup_grammar_persistence_test_env():
    """Isolate grammar persistence AND the config path for every test, so no test can leak
    files into mock-derived paths."""
    from plugin.framework import config as config_mod
    from plugin.framework import logging as logging_mod
    from plugin.writer.locale import grammar_persistence
    import logging
    import shutil
    import tempfile

    # Reset doc instances to ensure fresh initialization per test
    old_doc_instances = dict(grammar_persistence.grammar_registry.doc_persistence_instances)
    grammar_persistence.grammar_registry.doc_persistence_instances.clear()

    # Save logging state
    old_debug_log_path = logging_mod._debug_log_path
    old_enable_agent_log = logging_mod._enable_agent_log
    old_log_level_numeric = logging_mod._log_level_numeric

    # Save log handlers
    wa_logger = logging.getLogger("writeragent")
    root_logger = logging.getLogger()
    old_wa_handlers = list(wa_logger.handlers)
    old_root_handlers = list(root_logger.handlers)

    tmp_dir = tempfile.mkdtemp()
    # Seed the resolved-config-path cache too. Patching the user_config_dir attribute below does
    # NOT reach callers that bound it via `from plugin.framework.config import user_config_dir`
    # (e.g. plugin/chatbot/memory.py) — those still resolve the path from the mocked UNO ctx, and
    # MagicMock's default __fspath__ yields a relative "MagicMock/<name>/<id>" path that
    # MemoryStore's makedirs then creates inside the repo. Seeding the module-level cache routes
    # every resolver through this per-test temp dir instead.
    # Save the path before reset — reset_config_for_tests() clears _resolved_config_path.
    old_resolved = config_mod._resolved_config_path
    config_mod.reset_config_for_tests()
    config_mod._resolved_config_path = os.path.join(tmp_dir, "writeragent.json")
    try:
        with patch("plugin.framework.config.user_config_dir", return_value=tmp_dir):
            yield
    finally:
        config_mod.reset_config_for_tests()
        config_mod._resolved_config_path = old_resolved

        # Restore logging state
        logging_mod._debug_log_path = old_debug_log_path
        logging_mod._enable_agent_log = old_enable_agent_log
        logging_mod._log_level_numeric = old_log_level_numeric

        # Close and remove any handlers added during the test
        for h in list(wa_logger.handlers):
            if h not in old_wa_handlers:
                wa_logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        for h in list(root_logger.handlers):
            if h not in old_root_handlers:
                root_logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass

        # Clean up
        grammar_persistence.grammar_registry.doc_persistence_instances.clear()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        grammar_persistence.grammar_registry.doc_persistence_instances.update(old_doc_instances)



def _is_xdist_worker(session) -> bool:
    """xdist workers have ``config.workerinput``; the controller and plain pytest do not."""
    return getattr(session.config, "workerinput", None) is not None


def _repo_magic_mock_dir() -> str:
    # tests/conftest.py sits one level below the repo root (older triple dirname
    # pointed at the parent of the repo and never cleaned anything).
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "MagicMock")


_pytest_progress_done = 0
_pytest_progress_last_nodeid = ""
_pytest_progress_inflight: set[str] = set()
_pytest_progress_last_emit = 0.0
_pytest_progress_stop = None
_pytest_progress_thread = None

# After 7800 the next multiple of 100 is 8000, but GHA collects ~7988 items.
# PR #823 pull_request jobs were SIGTERM-cancelled after a quiet 7900s tail
# (35666812986) or after a logstart flood stalled the controller (35669866244).
# Same SHAs finish that tail in ~5s on workflow_dispatch (35668304406,
# 35669812792). last= was the last reported nodeid, not a hung hypothesis
# test — name leftover inflight tests on the idle line instead.
_PYTEST_PROGRESS_TAIL_AFTER = 7800
_PYTEST_PROGRESS_IDLE_SEC = 15.0
_PYTEST_PROGRESS_IDLE_LEFTOVER_CAP = 6


def format_idle_pytest_progress(done: int, last_nodeid: str, inflight: list[str]) -> str:
    """Idle heartbeat. leftover= is started-not-finished; last= is last reported."""
    if inflight:
        shown = ",".join(inflight[:_PYTEST_PROGRESS_IDLE_LEFTOVER_CAP])
        extra = len(inflight) - _PYTEST_PROGRESS_IDLE_LEFTOVER_CAP
        more = f" +{extra}" if extra > 0 else ""
        return f"pytest: {done} still-running leftover={shown}{more}"
    return f"pytest: {done} still-running last={last_nodeid or '-'}"


def should_emit_pytest_progress_count(done: int, *, failed: bool = False) -> bool:
    """When to print a ``pytest: N`` count heartbeat.

    Every 100 keeps the log small. After ``_PYTEST_PROGRESS_TAIL_AFTER`` also
    emit every 10 so the last 1% is not a silent gap.
    """
    if failed:
        return True
    if done <= 0:
        return False
    if done % 100 == 0:
        return True
    return done >= _PYTEST_PROGRESS_TAIL_AFTER and done % 10 == 0


def _emit_make_pytest_progress(msg: str) -> None:
    """Full lines on stderr. Pytest/xdist otherwise rewrite one ``\\r`` status line.

    Prefix a newline so the message is not glued onto a row of unwrapped dots
    when stdout and stderr are merged (Make ``2>&1``, some IDE captures).
    """
    global _pytest_progress_last_emit
    sys.stderr.write("\n" + msg + "\n")
    sys.stderr.flush()
    _pytest_progress_last_emit = time.monotonic()


def _make_pytest_progress_enabled() -> bool:
    return os.environ.get("WRITERAGENT_PYTEST_PROGRESS") == "1" and not os.environ.get(
        "PYTEST_XDIST_WORKER"
    )


def _idle_pytest_progress_loop() -> None:
    """Re-announce the last count if the tail goes quiet (one slow leftover test)."""
    stop = _pytest_progress_stop
    if stop is None:
        return
    while not stop.wait(_PYTEST_PROGRESS_IDLE_SEC):
        done = _pytest_progress_done
        nodeid = _pytest_progress_last_nodeid
        last = _pytest_progress_last_emit
        if done and (time.monotonic() - last) >= _PYTEST_PROGRESS_IDLE_SEC:
            leftover = sorted(_pytest_progress_inflight)
            _emit_make_pytest_progress(
                format_idle_pytest_progress(done, nodeid, leftover)
            )


def _start_idle_pytest_progress() -> None:
    global _pytest_progress_stop, _pytest_progress_thread
    import threading

    _stop_idle_pytest_progress()
    _pytest_progress_stop = threading.Event()
    _pytest_progress_thread = threading.Thread(
        target=_idle_pytest_progress_loop,
        name="pytest-progress-idle",
        daemon=True,
    )
    _pytest_progress_thread.start()


def _stop_idle_pytest_progress() -> None:
    global _pytest_progress_stop, _pytest_progress_thread
    stop = _pytest_progress_stop
    if stop is not None:
        stop.set()
    thread = _pytest_progress_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)
    _pytest_progress_stop = None
    _pytest_progress_thread = None


def pytest_configure(config):
    """Arm opt-in CI hang diagnostics (no-op unless WRITERAGENT_CI_DEBUG=1)."""
    from tests.harness.ci_debug import start_ci_debug

    start_ci_debug()


def pytest_runtest_logstart(nodeid, location):
    from tests.harness.ci_debug import log_ci_debug

    log_ci_debug(f"start {nodeid}")
    global _pytest_progress_last_nodeid
    if _make_pytest_progress_enabled():
        # Remember the name for the 15s idle line. Do not print every start:
        # after 7800 that was hundreds of parametrized word_diff_split lines
        # in ~1s (35669866244) and the controller stalled on GHA log I/O.
        _pytest_progress_last_nodeid = nodeid
        _pytest_progress_inflight.add(nodeid)


def pytest_runtest_logfinish(nodeid, location):
    from tests.harness.ci_debug import log_ci_debug

    log_ci_debug(f"end {nodeid}")
    _pytest_progress_inflight.discard(nodeid)


def pytest_sessionstart(session):
    """Clean leftover ``MagicMock/`` dirs from accidental mock stringification.

    Autouse config-path isolation prevents new litter. Sweep only on the controller:
    under xdist every worker also runs this hook, and concurrent rmtree of the same
    path races.
    """
    if _is_xdist_worker(session):
        return
    import shutil

    if _make_pytest_progress_enabled():
        global _pytest_progress_done, _pytest_progress_last_nodeid
        _pytest_progress_done = 0
        _pytest_progress_last_nodeid = ""
        _pytest_progress_inflight.clear()
        _emit_make_pytest_progress("pytest: starting (workers collecting…)")
        _start_idle_pytest_progress()
    magic_mock_dir = _repo_magic_mock_dir()
    if os.path.isdir(magic_mock_dir):
        shutil.rmtree(magic_mock_dir, ignore_errors=True)


def pytest_collection_finish(session):
    if _make_pytest_progress_enabled():
        _emit_make_pytest_progress(f"pytest: collected {len(session.items)} tests")


def pytest_runtest_logreport(report):
    """Heartbeat while xdist runs: dots/percent live on one \\r line and never appear under Make."""
    global _pytest_progress_done, _pytest_progress_last_nodeid
    if not _make_pytest_progress_enabled():
        return
    if getattr(report, "when", None) != "call":
        return
    _pytest_progress_done += 1
    _pytest_progress_last_nodeid = report.nodeid
    if should_emit_pytest_progress_count(_pytest_progress_done, failed=bool(report.failed)):
        if report.failed:
            suffix = f" FAIL {report.nodeid}"
        elif _pytest_progress_done % 100 == 0:
            suffix = ""
        else:
            suffix = f" last={report.nodeid}"
        _emit_make_pytest_progress(f"pytest: {_pytest_progress_done}{suffix}")


def _shutdown_harper_if_loaded() -> None:
    """Kill leftover harper-ls clients so an xdist worker can exit on Windows."""
    harper_mod = sys.modules.get("plugin.writer.locale.harper")
    if harper_mod is None:
        return
    shutdown = getattr(harper_mod, "shutdown_harper_runtime", None)
    if callable(shutdown):
        shutdown()


@pytest.fixture(autouse=True)
def _shutdown_harper_runtime_after_test():
    """Unit tests must not leave a harper-ls subprocess for worker teardown."""
    yield
    _shutdown_harper_if_loaded()


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if isolation leaked a ``MagicMock/`` tree under the repo root."""
    from tests.harness.ci_debug import stop_ci_debug

    stop_ci_debug()
    _shutdown_harper_if_loaded()
    if _is_xdist_worker(session):
        return
    if _make_pytest_progress_enabled():
        _stop_idle_pytest_progress()
        _emit_make_pytest_progress(
            f"pytest: done {_pytest_progress_done} exit={exitstatus}"
        )
    if os.path.isdir(_repo_magic_mock_dir()):
        session.exitstatus = 1
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                "MagicMock/ exists under the repo root after the suite — config path isolation leaked",
                red=True,
            )
