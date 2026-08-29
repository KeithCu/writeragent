#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Mini in-process test runner (no pytest dependency).
#
# This module can be called from:
# - Inside LibreOffice (given a UNO ComponentContext)
# - Outside LibreOffice via officehelper.bootstrap() to get a ctx
#
# It aggregates existing in-LO tests (Writer/Calc, etc.) and returns
# a JSON summary that external tools or agents can consume.

import logging
import json
import os
import shutil
import sys
import traceback
import unittest
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence

log = logging.getLogger(__name__)

# CLI path/name filters from ``python -m plugin.testing_runner <filter>``.
# File-path filters select modules; leftover ``test_*`` names select functions.
_cli_filters: list[str] = []


def _progress(msg: str) -> None:
    """Print a line immediately so soffice aborts still name the last test."""
    print(msg, file=sys.stderr, flush=True)


def _soffice_pids() -> str:
    """Best-effort soffice.bin PIDs for correlating glibc aborts with this run."""
    try:
        import subprocess

        out = subprocess.check_output(
            ["pgrep", "-x", "soffice.bin"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        pids = ",".join(out.split())
        return pids or "-"
    except Exception:
        return "-"


def _test_function_filters(filters: Sequence[str]) -> list[str]:
    """Return CLI tokens that name a test function rather than a module path."""
    names: list[str] = []
    for token in filters:
        if "/" in token or "\\" in token or token.endswith(".py"):
            continue
        if token.endswith("_uno"):
            continue
        if token.startswith("test_"):
            names.append(token)
    return names


def _module_matches_filters(full_path: str, filename: str, filters: Sequence[str]) -> bool:
    """True if this UNO file should load given CLI filters (path or ``def test_``)."""
    if not filters:
        return True
    if any(token in full_path or token in filename for token in filters):
        return True
    func_filters = _test_function_filters(filters)
    if not func_filters:
        return False
    try:
        source = Path(full_path).read_text(encoding="utf-8")
    except OSError:
        return False
    return any(f"def {name}" in source for name in func_filters)

# Flag to run UNO chart tests with visible window rather than hidden
show_window: bool = False
# Packet F mock-sidebar: visible soffice with the developer's real user profile.
use_user_profile: bool = False

# Only these modules run under ``--user-profile`` (and they are skipped otherwise).
_USER_PROFILE_ONLY_UNO = frozenset({"test_mock_llm_sidebar_uno.py"})


def _parse_cli_args(argv: Sequence[str]) -> list[str]:
    """Split runner flags from suite filters. Sets ``show_window`` / ``use_user_profile``.

    ``python -m plugin.testing_runner`` runs as ``__main__``, so tests that
    ``import plugin.testing_runner`` would miss module-level flags. Mirror
    ``--user-profile`` into ``WRITERAGENT_UNO_USER_PROFILE`` as well.
    """
    global show_window, use_user_profile
    filters: list[str] = []
    for arg in argv:
        if arg in ("--visible", "--show-window"):
            show_window = True
        elif arg == "--user-profile":
            use_user_profile = True
            show_window = True
            os.environ["WRITERAGENT_UNO_USER_PROFILE"] = "1"
        else:
            filters.append(str(arg))
    if os.environ.get("WRITERAGENT_UNO_USER_PROFILE") == "1":
        use_user_profile = True
        show_window = True
    return filters


def _soffice_bootstrap_command(officehelper_module: Any) -> str | None:
    """Return a soffice command for native tests.

    Default: headless + throwaway ``UserInstallation`` (never recovery UI).
    ``--user-profile``: visible, real user install. ``--norestore`` skips the
    crash-recovery dialog; tests open WriterAgentDeck over UNO instead.
    """
    try:
        program_dir = Path(officehelper_module.__file__).resolve().parent
        soffice = program_dir / ("soffice.exe" if __import__("sys").platform.startswith("win") else "soffice")
        if not soffice.exists():
            return None
        quoted = '"%s"' % soffice
        if use_user_profile:
            # Keep the developer's UserInstallation (extension + writeragent.json).
            # Actual GUI start is ``_user_profile_soffice_argv`` (adds --writer, no --nodefault).
            return "%s --norestore --nofirststartwizard --nocrashreport --writer" % quoted
        profile_url = Path(tempfile.mkdtemp(prefix="writeragent-lo-test-profile-")).as_uri()
        return (
            "%s --headless --norestore --nofirststartwizard --nocrashreport "
            "-env:UserInstallation=%s" % (quoted, profile_url)
        )
    except Exception:
        return None


_SOFFICE_STRIP_ENV = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")


def _child_env_without_runner_python(*, uno_thread_guard: bool | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in _SOFFICE_STRIP_ENV:
        env.pop(key, None)
    # URP dispatch of WriterAgentDeck runs getRealInterface off the VCL thread.
    # Dev thread_guard would abort ChatPanel create (Dummy-N). Official opt-out.
    if uno_thread_guard is False:
        env["WRITERAGENT_UNO_THREAD_GUARD"] = "0"
    return env


def _user_profile_soffice_argv(soffice: Path, accept: str) -> list[str]:
    """Visible Writer on the real user profile. No ``--nodefault`` (officehelper crash).

    ``--norestore`` skips the crash-recovery dialog that otherwise blocks the UNO pipe.
    Tests then show ``WriterAgentDeck`` over UNO (View → Sidebar may be off).
    """
    return [
        str(soffice),
        "--norestore",
        "--nofirststartwizard",
        "--nocrashreport",
        "--nologo",
        "--writer",
        "--accept=%s" % accept,
    ]


def _libreoffice_user_lock_path() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", "")) / "LibreOffice" / "4"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "LibreOffice" / "4"
    else:
        base = Path.home() / ".config" / "libreoffice" / "4"
    return base / ".lock"


def _soffice_bin_running() -> bool:
    try:
        import subprocess

        return (
            subprocess.call(
                ["pgrep", "-x", "soffice.bin"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            == 0
        )
    except Exception:
        return False


def _clear_stale_user_profile_ipc() -> None:
    """Drop leftover SingleOfficeIPC pipes and ``.lock`` when soffice is not running.

    A stale UserInstallation ``.lock`` with ``IPCServer=false`` makes the next
    soffice skip ``--accept``, so Packet F cannot connect.
    """
    if _soffice_bin_running():
        return
    lock = _libreoffice_user_lock_path()
    try:
        if lock.is_file():
            lock.unlink()
    except OSError:
        pass
    if not hasattr(os, "getuid"):
        return
    import glob

    tmp = tempfile.gettempdir()
    for path in glob.glob(os.path.join(tmp, "OSL_PIPE_%s_*" % os.getuid())):
        try:
            os.unlink(path)
        except OSError:
            pass


def _unused_tcp_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resolve_soffice_bin(officehelper_module: Any) -> Path | None:
    name = "soffice.exe" if sys.platform.startswith("win") else "soffice"
    next_to_helper = Path(officehelper_module.__file__).resolve().parent / name
    if next_to_helper.exists():
        return next_to_helper
    which = shutil.which(name)
    if which:
        return Path(which)
    for candidate in (
        Path("/usr/lib/libreoffice/program") / name,
        Path("/snap/libreoffice/current/lib/libreoffice/program") / name,
        Path("/Applications/LibreOffice.app/Contents/MacOS") / name,
    ):
        if candidate.exists():
            return candidate
    return None


def _bootstrap_user_profile_gui(officehelper_module: Any) -> Any:
    """Visible soffice like ``make lo-start``: user profile, ``--norestore --writer``, UNO pipe.

    ``officehelper.bootstrap`` always appends ``--nodefault --nologo --accept=pipe``.
    That path opened a window and then crashed (URP disposed). This start matches
    ``scripts/launch-lo-debug.sh`` plus an accept string so tests can attach.
    """
    import subprocess
    import time

    import uno
    from com.sun.star.connection import NoConnectException

    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("user-profile sidebar tests need DISPLAY (visible Writer)")
    soffice = _resolve_soffice_bin(officehelper_module)
    if soffice is None:
        raise RuntimeError("soffice not found (PATH, officehelper dir, or common install paths)")
    _clear_stale_user_profile_ipc()
    port = _unused_tcp_port()
    accept = "socket,host=127.0.0.1,port=%s;urp;" % port
    cmd = _user_profile_soffice_argv(soffice, accept)
    proc = subprocess.Popen(
        cmd,
        env=_child_env_without_runner_python(uno_thread_guard=False),
        start_new_session=True,
    )
    local = uno.getComponentContext()
    smgr = getattr(local, "getServiceManager", lambda: None)()
    if smgr is None:
        smgr = getattr(local, "ServiceManager", None)
    if smgr is None:
        raise RuntimeError("no ServiceManager on local UNO context")
    resolver = smgr.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local
    )
    url = "uno:%sStarOffice.ComponentContext" % accept
    last_exc: BaseException | None = None
    # GUI + extension OnStartApp is slower than headless officehelper.
    for delay in (0.5, 1, 1, 2, 2, 3, 5, 8, 8):
        time.sleep(delay)
        code = proc.poll()
        if code is not None:
            raise RuntimeError(
                "user-profile soffice exited %s before UNO connect (crash recovery or mixed PYTHONPATH?)"
                % code
            )
        try:
            return resolver.resolve(url)
        except NoConnectException as exc:
            last_exc = exc
    raise RuntimeError("could not connect to user-profile soffice: %s" % last_exc)


def _bootstrap_office(officehelper_module: Any) -> Any:
    """Start soffice without leaking the test runner's Python env into the child.

    Visible user-profile soffice loads the installed WriterAgent OXT. If it
    inherits the checkout ``PYTHONPATH``, the extension imports mixed sources
    and can crash on startup (URP then reports the bridge disposed).
    """
    if use_user_profile:
        return _bootstrap_user_profile_gui(officehelper_module)
    saved: dict[str, str] = {}
    for key in _SOFFICE_STRIP_ENV:
        val = os.environ.pop(key, None)
        if val is not None:
            saved[key] = val
    try:
        return officehelper_module.bootstrap(soffice=_soffice_bootstrap_command(officehelper_module))
    finally:
        os.environ.update(saved)



def native_test(func):
    """Decorator to mark a function as a test in the native test runner.

    Note: pytest-based runs will automatically skip/ignore these via a hook
    in conftest.py to keep the 'skipped' count meaningful.
    """
    func._is_test = True
    return func


def setup(func):
    """Decorator to mark a function as the setup routine for a test module."""
    func._is_setup = True
    return func


def teardown(func):
    """Decorator to mark a function as the teardown routine for a test module."""
    func._is_teardown = True
    return func


def _run_suite(ctx: Any, suites: List[Dict[str, Any]], name: str, module, *args) -> tuple[int, int]:
    """Run a test module using the decorator-based native runner.

    Collects functions marked with @setup, @teardown, and @native_test.
    Executes setup(ctx), then all tests(ctx), then teardown(ctx).
    Returns (passed, failed) for top-level aggregation.
    """
    passed, failed, suite_log = run_module_suite(ctx, module, name, *args)
    entry: Dict[str, Any] = {"name": name, "log": suite_log}
    if failed:
        entry["failed"] = failed
    suites.append(entry)
    return passed, failed


def _is_uno_bridge_disposed(exc: Exception) -> bool:
    return type(exc).__name__ == "DisposedException" or "Binary URP bridge" in str(exc)


def run_module_suite(ctx, module, name, doc_model=None):
    """Monolithic entry point for running a test module (legacy/menu support).
    Returns (passed, failed, log).
    """
    log.info(f"run_module_suite start: {name}")
    total_passed = 0
    total_failed = 0
    suite_log = []

    setup_func = None
    teardown_func = None
    test_funcs = []

    # Discover decorators, iterating over module dict to preserve insertion (definition) order
    for _unused, attr in module.__dict__.items():
        if callable(attr):
            # `MagicMock` returns truthy values for any attribute access, so we must
            # check for an explicit boolean marker set by our decorators.
            if getattr(attr, "_is_setup", False) is True:
                setup_func = attr
            elif getattr(attr, "_is_teardown", False) is True:
                teardown_func = attr
            elif getattr(attr, "_is_test", False) is True:
                test_funcs.append(attr)

    # Discovery fallback: if no @test functions, check for old run_*_tests approach
    if not test_funcs:
        fallback_func_name = f"run_{name.split('.')[-1].replace('_tests', '').replace('test_', '')}_tests"
        if "calc.tests" in name:
            fallback_func_name = "run_calc_tests"
        elif "draw.tests" in name:
            fallback_func_name = "run_draw_tests"

        fallback_func = getattr(module, fallback_func_name, None)
        if fallback_func:
            try:
                p, f, lines = fallback_func(ctx, doc_model)
                return int(p or 0), int(f or 0), list(lines or [])
            except Exception as e:
                return 0, 1, [f"EXCEPTION in {fallback_func_name}: {e}", traceback.format_exc()]

    _progress(f"SUITE start {name} python_pid={os.getpid()} soffice.bin={_soffice_pids()}")
    try:
        if setup_func:
            import inspect

            try:
                sig = inspect.signature(setup_func)
                expects_ctx = any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL) for p in sig.parameters.values())
            except Exception:
                expects_ctx = True
            if expects_ctx:
                setup_func(ctx)
            else:
                setup_func()

        name_filters = _test_function_filters(_cli_filters)
        for test_func in test_funcs:
            if name_filters and not any(token in test_func.__name__ for token in name_filters):
                continue
            test_line = f"Running test: {test_func.__name__}"
            _progress(f"TEST start {name}.{test_func.__name__}")
            try:
                # After suite @setup removal, native tests take ctx (and often doc via
                # @with_native_doc). Pass ctx when the signature accepts it; no-arg
                # tests (pure schema checks) stay parameterless.
                import inspect

                try:
                    sig = inspect.signature(test_func)
                    accepts_ctx = "ctx" in sig.parameters
                except Exception:
                    accepts_ctx = True
                if accepts_ctx:
                    test_func(ctx=ctx)
                else:
                    test_func()
                total_passed += 1
                suite_log.append(f"{test_line} — OK")
                _progress(f"TEST end {name}.{test_func.__name__} OK")
            except ModuleNotFoundError as e:
                # Some "native" tests attempt to use pytest.skip, but LibreOffice's
                # Python may not have pytest installed.
                if getattr(e, "name", None) == "pytest":
                    suite_log.append(f"{test_line} — SKIP (pytest not available)")
                    _progress(f"TEST end {name}.{test_func.__name__} SKIP")
                    continue
                total_failed += 1
                suite_log.append(f"{test_line} — FAIL (ModuleNotFoundError: {e})")
                suite_log.append(traceback.format_exc())
                _progress(f"TEST end {name}.{test_func.__name__} FAIL")
            except unittest.SkipTest as e:
                total_passed += 1
                suite_log.append(f"{test_line} — OK (skipped) ({e})")
                _progress(f"TEST end {name}.{test_func.__name__} SKIP")
            except AssertionError as e:
                total_failed += 1
                suite_log.append(f"{test_line} — FAIL (AssertionError: {e})")
                suite_log.append(traceback.format_exc())
                _progress(f"TEST end {name}.{test_func.__name__} FAIL")
            except Exception as e:
                total_failed += 1
                suite_log.append(f"{test_line} — FAIL ({type(e).__name__}: {e})")
                suite_log.append(traceback.format_exc())
                _progress(f"TEST end {name}.{test_func.__name__} FAIL")

    except Exception as e:
        total_failed += 1
        suite_log.append(f"SUITE ABORTED EXCEPTION: {e}")
        suite_log.append(traceback.format_exc())
    finally:
        if teardown_func:
            try:
                import inspect

                try:
                    sig = inspect.signature(teardown_func)
                    expects_ctx = any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL) for p in sig.parameters.values())
                except Exception:
                    expects_ctx = True
                if expects_ctx:
                    teardown_func(ctx)
                else:
                    teardown_func()
            except Exception as e:
                if _is_uno_bridge_disposed(e):
                    suite_log.append(f"TEARDOWN SKIPPED: UNO bridge disposed ({e})")
                else:
                    total_failed += 1
                    suite_log.append(f"TEARDOWN EXCEPTION: {e}")
                    suite_log.append(traceback.format_exc())

    _progress(f"SUITE end {name} passed={total_passed} failed={total_failed}")
    return total_passed, total_failed, suite_log


def run_all_tests(ctx: Any) -> str:
    """Run all in-process WriterAgent tests and return a JSON summary string.

    The JSON structure is:
        {
          "total_passed": int,
          "total_failed": int,  # omitted when zero
          "suites": [
            {
              "name": "writer.format_tests",
              "failed": int,  # omitted when zero
              "log": ["Running test: foo — OK", "Running test: bar — FAIL (...)", ...]
            },
            ...
          ]
        }

    This is intentionally minimal and self-contained so we don't need pytest
    inside LibreOffice. External callers can parse this JSON, print a report,
    and use total_failed as an exit code condition.
    """
    # Mock doc.agent_edit_review_mode during tests to default to "off"
    # and only track its test-specific overrides in memory.
    import plugin.framework.config
    original_get_config = plugin.framework.config.get_config
    original_set_config = plugin.framework.config.set_config
    original_get_config_dict = plugin.framework.config.get_config_dict

    _review_mode_override: Dict[str, Any] = {}

    def test_get_config(key):
        if key == "doc.agent_edit_review_mode":
            return _review_mode_override.get(key, "off")
        return original_get_config(key)

    def test_set_config(key, value):
        if key == "doc.agent_edit_review_mode":
            _review_mode_override[key] = value
            from plugin.framework.event_bus import global_event_bus
            global_event_bus.emit("config:changed", ctx=ctx)
            return
        original_set_config(key, value)

    def test_get_config_dict():
        base = original_get_config_dict()
        merged = dict(base)
        merged["doc.agent_edit_review_mode"] = _review_mode_override.get("doc.agent_edit_review_mode", "off")
        return merged

    setattr(plugin.framework.config, "get_config", test_get_config)
    setattr(plugin.framework.config, "set_config", test_set_config)
    setattr(plugin.framework.config, "get_config_dict", test_get_config_dict)

    suites: List[Dict[str, Any]] = []


    total_passed = 0
    total_failed = 0


    # Try to reuse an existing active document when it matches the suite type;
    # otherwise the underlying helpers will create their own temporary docs.
    try:
        from plugin.framework.uno_context import get_active_document

        model = get_active_document(ctx)
    except ImportError:
        model = None

    def _doc_type_never(model: Any) -> bool:
        return False

    is_writer_fn: Callable[[Any], bool]
    is_calc_fn: Callable[[Any], bool]
    is_draw_fn: Callable[[Any], bool]
    try:
        from plugin.doc.doc_type import is_writer, is_calc, is_draw

        is_writer_fn, is_calc_fn, is_draw_fn = is_writer, is_calc, is_draw
    except ImportError:
        is_writer_fn = is_calc_fn = is_draw_fn = _doc_type_never

    writer_doc = model if (model is not None and is_writer_fn(model)) else None
    calc_doc = model if (model is not None and is_calc_fn(model)) else None
    draw_doc = model if (model is not None and is_draw_fn(model)) else None
    keeper_doc = None

    try:
        from plugin.framework.uno_context import get_desktop
        import uno

        hidden_prop = uno.createUnoStruct(
            "com.sun.star.beans.PropertyValue",
            Name="Hidden",
            Value=True,
        )
        # External Windows bootstraps can shut down LibreOffice when a suite
        # closes its only hidden document. Keep one document alive until all
        # native suites finish so the UNO bridge stays valid across modules.
        # User-profile sidebar tests must not open a hidden Writer — that steals
        # the restored deck / current component.
        if not use_user_profile:
            keeper_doc = get_desktop(ctx).loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    except Exception as e:
        log.warning("run_all_tests: could not create keeper document: %s", e)

    # Initialize the tool registry (Writer/Calc/Draw modules) before loading any
    # UNO test file. Each suite below snapshots/restores sys.modules (uno, com,
    # …); if the first suite only pulled in a partial UNO graph, a later suite's
    # first ``get_tools()`` could otherwise see an empty registry or hit import
    # edge cases. Extension startup already sets ``_initialized``; this is a
    # no-op then.
    try:
        from plugin.framework.uno_context import set_fallback_ctx

        set_fallback_ctx(ctx)
        from plugin.framework.config import init_config

        init_config(ctx)
        # User-profile soffice already ran extension OnStartApp. Re-bootstrap
        # over URP has crashed the GUI; skip it for Packet F.
        if not use_user_profile:
            from plugin.main import bootstrap

            bootstrap(ctx=ctx)
    except Exception as e:
        log.warning("run_all_tests: bootstrap failed (in-LO tool tests may fail): %s", e)

    def _ensure_live_ctx(current_ctx: Any) -> Any:
        try:
            current_ctx.getServiceManager()
            return current_ctx
        except Exception:
            pass
        try:
            import officehelper

            new_ctx = _bootstrap_office(officehelper)
            from plugin.framework.uno_context import set_fallback_ctx

            set_fallback_ctx(new_ctx)
            from plugin.framework.config import init_config

            init_config(new_ctx)
            if not use_user_profile:
                from plugin.main import bootstrap

                bootstrap(ctx=new_ctx)
            return new_ctx
        except Exception as e:
            log.warning("run_all_tests: could not refresh disposed UNO context: %s", e)
            return current_ctx

    import os
    from plugin.framework.constants import get_plugin_dir
    import importlib.util

    tests_root = os.path.join(os.path.dirname(get_plugin_dir()), "tests")

    if os.path.isdir(tests_root):
        # Discover and run all test modules recursively in the tests directory.
        # UNO tests are identified by the _uno.py suffix or being in the legacy uno/ dir.
        from tests.testing_utils import NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS

        _MISSING = object()

        # Gather all candidates
        test_candidates = []
        import sys

        filter_strs = _parse_cli_args(sys.argv[1:])
        global _cli_filters
        _cli_filters = list(filter_strs)

        for root, _dirs, files in os.walk(tests_root):
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                # Match test_*.py or *_tests.py
                if not (filename.startswith("test_") or filename.endswith("_tests.py")):
                    continue
                
                # We specifically want tests that are meant for the native runner.
                # These are now identified by the _uno suffix or being in the legacy uno/ dir.
                is_uno_test = "_uno.py" in filename or "uno" in root.split(os.sep)
                if is_uno_test:
                    user_only = filename in _USER_PROFILE_ONLY_UNO
                    if user_only != use_user_profile:
                        continue
                    full_path = os.path.join(root, filename)
                    if _module_matches_filters(full_path, filename, filter_strs):
                        test_candidates.append(full_path)

        for module_path in sorted(test_candidates):
            ctx = _ensure_live_ctx(ctx)
            filename = os.path.basename(module_path)
            module_name = filename[:-3]
            
            # Construct a unique module name for sys.modules to avoid collisions
            # during the recursive walk.
            rel_path = os.path.relpath(module_path, tests_root)
            sys_module_name = "plugin.tests." + rel_path[:-3].replace(os.sep, ".")

            restore_snapshot: Dict[str, Any] | None = None
            try:
                restore_snapshot = {k: sys.modules.get(k, _MISSING) for k in NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS}
                spec = importlib.util.spec_from_file_location(sys_module_name, module_path)
                if spec is None or spec.loader is None:
                    continue
                test_module = importlib.util.module_from_spec(spec)
                sys.modules[sys_module_name] = test_module
                spec.loader.exec_module(test_module)

                # Menu-only facade (e.g. calc ``test_calc_uno``): aggregates other UNO
                # modules via ``run_calc_tests`` / ``run_integration_tests`` and must not
                # run here — ``'_uno.py' in filename`` matches it, but it has no
                # ``@native_test`` and the generic fallback name would not map to those runners.
                if getattr(test_module, "SKIP_NATIVE_RUN_ALL", False):
                    continue

                doc_to_pass = None
                if "writer" in module_name or "format" in module_name:
                    # Writer core tests mutate the document and assume an empty starting state,
                    # so we pass None to force it to create its own hidden temporary document.
                    if "test_writer" not in module_name or module_name == "test_writer_uno":
                        doc_to_pass = writer_doc
                elif "calc" in module_name:
                    doc_to_pass = calc_doc
                elif "draw" in module_name or "impress" in module_name:
                    doc_to_pass = draw_doc

                p, f = _run_suite(ctx, suites, sys_module_name.replace("plugin.tests.", ""), test_module, doc_to_pass)
                total_passed += p
                total_failed += f
            except ImportError as e:
                print(f"Skipping {filename} due to ImportError: {e}")
            except Exception as e:
                print(f"Error loading {filename}: {e}")
            finally:
                # Prevent sys.modules mocking from polluting later native tests.
                if restore_snapshot is not None:
                    for k, v in restore_snapshot.items():
                        if v is _MISSING:
                            sys.modules.pop(k, None)
                        else:
                            sys.modules[k] = v

        if keeper_doc is not None:
            try:
                keeper_doc.close(True)
            except Exception:
                pass

    summary: Dict[str, Any] = {"total_passed": total_passed, "suites": suites}
    if total_failed:
        summary["total_failed"] = total_failed
    return json.dumps(summary, ensure_ascii=False, indent=2)


def main() -> int:
    """Command-line entrypoint: bootstrap LO and run tests.

    This lets you run tests from a normal shell without clicking menus:

        python -m plugin.testing_runner

    The import of officehelper/uno is done lazily so that this module
    can still be imported inside LibreOffice without pulling them in.
    """
    try:
        import officehelper
    except ImportError:
        print("ERROR: officehelper module is not available; run with LibreOffice's Python.", flush=True)
        return 1

    _parse_cli_args(sys.argv[1:])

    # Suppress MCP server startup in the soffice child process; it inherits
    # this env var and McpModule.start_background() checks it.
    #
    # WRITERAGENT_TESTING only short-circuits QueueExecutor inline execution; it does
    # NOT disable Layer A (WRITERAGENT_UNO_THREAD_GUARD). Use make lo-test-threadguard
    # to run this suite with the viral UNO proxy so worker-thread violations fail loudly.
    import os
    os.environ["WRITERAGENT_TESTING"] = "1"

    try:
        ctx = _bootstrap_office(officehelper)
    except Exception as e:
        # Typical in CI/headless shells: no soffice pipe (BootstrapException, NoConnectException, etc.)
        print(f"SKIP: LibreOffice UNO bootstrap failed; skipping in-LO tests.\n  ({type(e).__name__}: {e})", flush=True)
        return 0

    if ctx is None:
        print("ERROR: Could not bootstrap LibreOffice (officehelper.bootstrap() returned None).", flush=True)
        return 1

    summary_json = run_all_tests(ctx)
    print(summary_json, flush=True)

    try:
        summary = json.loads(summary_json)
    except Exception:
        summary = {"total_failed": 1}

    # Force-close LibreOffice via the Makefile/caller instead of in-process to avoid hangs.

    # Print a compact "tail" summary so callers can scan results quickly
    # even when the output above includes verbose tracebacks/log spam.
    total_passed = int(summary.get("total_passed", 0) or 0)
    total_failed = int(summary.get("total_failed", 0) or 0)
    print(f'"total_passed": {total_passed},', flush=True)
    if total_failed:
        print(f'"total_failed": {total_failed},', flush=True)

    return 0 if int(summary.get("total_failed", 0) or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
