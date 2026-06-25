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
import traceback
import unittest
from typing import Any, Callable, Dict, List

log = logging.getLogger(__name__)

# Flag to run UNO chart tests with visible window rather than hidden
show_window: bool = False



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
    for _, attr in module.__dict__.items():
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

        for test_func in test_funcs:
            test_line = f"Running test: {test_func.__name__}"
            try:
                # Pass doc_model if test_func accepts arguments, otherwise call normally
                # (Existing tests assume global _test_doc from setup)
                test_func()
                total_passed += 1
                suite_log.append(f"{test_line} — OK")
            except ModuleNotFoundError as e:
                # Some "native" tests attempt to use pytest.skip, but LibreOffice's
                # Python may not have pytest installed.
                if getattr(e, "name", None) == "pytest":
                    suite_log.append(f"{test_line} — SKIP (pytest not available)")
                    continue
                total_failed += 1
                suite_log.append(f"{test_line} — FAIL (ModuleNotFoundError: {e})")
                suite_log.append(traceback.format_exc())
            except unittest.SkipTest as e:
                total_passed += 1
                suite_log.append(f"{test_line} — OK (skipped) ({e})")
            except AssertionError as e:
                total_failed += 1
                suite_log.append(f"{test_line} — FAIL (AssertionError: {e})")
                suite_log.append(traceback.format_exc())
            except Exception as e:
                total_failed += 1
                suite_log.append(f"{test_line} — FAIL ({type(e).__name__}: {e})")
                suite_log.append(traceback.format_exc())

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
                total_failed += 1
                suite_log.append(f"TEARDOWN EXCEPTION: {e}")
                suite_log.append(traceback.format_exc())

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
        from plugin.doc.document_helpers import is_writer, is_calc, is_draw

        is_writer_fn, is_calc_fn, is_draw_fn = is_writer, is_calc, is_draw
    except ImportError:
        is_writer_fn = is_calc_fn = is_draw_fn = _doc_type_never

    writer_doc = model if (model is not None and is_writer_fn(model)) else None
    calc_doc = model if (model is not None and is_calc_fn(model)) else None
    draw_doc = model if (model is not None and is_draw_fn(model)) else None

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
        from plugin.main import bootstrap

        bootstrap(ctx=ctx)
    except Exception as e:
        log.warning("run_all_tests: bootstrap failed (in-LO tool tests may fail): %s", e)

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
        filter_strs = []
        for arg in sys.argv[1:]:
            if arg in ("--visible", "--show-window"):
                import plugin.testing_runner
                plugin.testing_runner.show_window = True
            else:
                filter_strs.append(arg)

        for root, dirs, files in os.walk(tests_root):
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
                    full_path = os.path.join(root, filename)
                    if not filter_strs or any(f in full_path or f in filename for f in filter_strs):
                        test_candidates.append(full_path)

        for module_path in sorted(test_candidates):
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

    # Suppress MCP server startup in the soffice child process; it inherits
    # this env var and McpModule.start_background() checks it.
    #
    # WRITERAGENT_TESTING only short-circuits QueueExecutor inline execution; it does
    # NOT disable Layer A (WRITERAGENT_UNO_THREAD_GUARD). Use make lo-test-threadguard
    # to run this suite with the viral UNO proxy so worker-thread violations fail loudly.
    import os
    os.environ["WRITERAGENT_TESTING"] = "1"

    try:
        ctx = officehelper.bootstrap()
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
