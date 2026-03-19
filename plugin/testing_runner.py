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

import sys
import json
import traceback
from typing import Any, Dict, List


def native_test(func):
    """Decorator to mark a function as a test in the native test runner."""
    func._is_test = True
    try:
        import pytest
        func = pytest.mark.skip(reason="Run by native runner only")(func)
    except ImportError:
        pass
    return func

def setup(func):
    """Decorator to mark a function as the setup routine for a test module."""
    func._is_setup = True
    return func

def teardown(func):
    """Decorator to mark a function as the teardown routine for a test module."""
    func._is_teardown = True
    return func

def _run_suite(
    ctx: Any,
    suites: List[Dict[str, Any]],
    name: str,
    module,
    *args,
) -> None:
    """Run a test module using the decorator-based native runner.

    Collects functions marked with @setup, @teardown, and @native_test.
    Executes setup(ctx), then all tests(ctx), then teardown(ctx).
    """
    passed, failed, log = run_module_suite(ctx, module, name, *args)
    suites.append({
        "name": name,
        "passed": passed,
        "failed": failed,
        "log": log,
    })


def run_module_suite(ctx, module, name, doc_model=None):
    """Monolithic entry point for running a test module (legacy/menu support).
    Returns (passed, failed, log).
    """
    from plugin.framework.logging import debug_log
    debug_log(f"run_module_suite start: {name}", context="Tests")
    total_passed = 0
    total_failed = 0
    log = []

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
                p, f, l = fallback_func(ctx, doc_model)
                return int(p or 0), int(f or 0), list(l or [])
            except Exception as e:
                return 0, 1, [f"EXCEPTION in {fallback_func_name}: {e}", traceback.format_exc()]

    try:
        if setup_func:
            setup_name = getattr(setup_func, "__name__", repr(setup_func))
            log.append(f"Running setup: {setup_name}")
            import inspect
            try:
                sig = inspect.signature(setup_func)
                expects_ctx = any(
                    p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                    for p in sig.parameters.values()
                ) or any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())
            except Exception:
                expects_ctx = True
            if expects_ctx:
                setup_func(ctx)
            else:
                setup_func()

        for test_func in test_funcs:
            try:
                log.append(f"Running test: {test_func.__name__}")
                # Pass doc_model if test_func accepts arguments, otherwise call normally
                # (Existing tests assume global _test_doc from setup)
                test_func()
                total_passed += 1
                log.append(f"OK: {test_func.__name__}")
            except ModuleNotFoundError as e:
                # Some "native" tests attempt to use pytest.skip, but LibreOffice's
                # Python may not have pytest installed.
                if getattr(e, "name", None) == "pytest":
                    log.append(f"SKIP: {test_func.__name__} (pytest not available)")
                    continue
                total_failed += 1
                log.append(f"FAIL: {test_func.__name__} (ModuleNotFoundError: {e})")
                log.append(traceback.format_exc())
            except AssertionError as e:
                total_failed += 1
                log.append(f"FAIL: {test_func.__name__} (AssertionError: {e})")
                log.append(traceback.format_exc())
            except Exception as e:
                total_failed += 1
                log.append(f"FAIL: {test_func.__name__} (Exception: {e})")
                log.append(traceback.format_exc())

    except Exception as e:
        total_failed += 1
        log.append(f"SUITE ABORTED EXCEPTION: {e}")
        log.append(traceback.format_exc())
    finally:
        if teardown_func:
            try:
                teardown_name = getattr(teardown_func, "__name__", repr(teardown_func))
                log.append(f"Running teardown: {teardown_name}")
                import inspect
                try:
                    sig = inspect.signature(teardown_func)
                    expects_ctx = any(
                        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                        for p in sig.parameters.values()
                    ) or any(
                        p.kind == p.VAR_POSITIONAL for p in sig.parameters.values()
                    )
                except Exception:
                    expects_ctx = True
                if expects_ctx:
                    teardown_func(ctx)
                else:
                    teardown_func()
            except Exception as e:
                total_failed += 1
                log.append(f"TEARDOWN EXCEPTION: {e}")
                log.append(traceback.format_exc())

    return total_passed, total_failed, log


def run_all_tests(ctx: Any) -> str:
    """Run all in-process WriterAgent tests and return a JSON summary string.

    The JSON structure is:
        {
          "total_passed": int,
          "total_failed": int,
          "suites": [
            {
              "name": "writer.format_tests",
              "passed": int,
              "failed": int,
              "log": ["OK: ...", "FAIL: ...", ...]
            },
            ...
          ]
        }

    This is intentionally minimal and self-contained so we don't need pytest
    inside LibreOffice. External callers can parse this JSON, print a report,
    and use total_failed as an exit code condition.
    """
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

    try:
        from plugin.framework.document import is_writer, is_calc, is_draw
    except ImportError:
        def is_writer(m): return False
        def is_calc(m): return False
        def is_draw(m): return False

    writer_doc = model if (model is not None and is_writer(model)) else None
    calc_doc = model if (model is not None and is_calc(model)) else None
    draw_doc = model if (model is not None and is_draw(model)) else None

    import os
    import importlib.util

    tests_dir = os.path.join(os.path.dirname(__file__), "tests", "uno")

    if os.path.isdir(tests_dir):
        # Discover and run all test modules in the tests/uno directory
        # Some "native" test modules are actually designed for plain Python and
        # temporarily replace sys.modules entries like `com.sun.star.*` with
        # MagicMocks. Since this runner imports multiple test modules into the
        # same interpreter, those mocks must not leak across module boundaries.
        _SYS_MODULE_KEYS_TO_RESTORE = [
            # UNO / bridge related (used by multiple uno test modules)
            "uno",
            "unohelper",
            # UNO "com.sun.star.*" namespaces
            "com",
            "com.sun",
            "com.sun.star",
            "com.sun.star.awt",
            "com.sun.star.ui",
            "com.sun.star.ui.UIElementType",
            "com.sun.star.task",
            # Some test modules also mock the project "core.*" modules.
            "core",
            "core.logging",
            "core.async_stream",
            "core.config",
            "core.api",
            "core.document",
            "core.document_tools",
            "core.constants",
        ]
        _MISSING = object()

        for filename in sorted(os.listdir(tests_dir)):
            if (filename.startswith("test_") or filename.endswith("_tests.py")) and filename.endswith(".py"):
                module_name = filename[:-3]
                module_path = os.path.join(tests_dir, filename)

                try:
                    restore_snapshot = None
                    restore_snapshot = {
                        k: sys.modules.get(k, _MISSING) for k in _SYS_MODULE_KEYS_TO_RESTORE
                    }
                    spec = importlib.util.spec_from_file_location(f"plugin.tests.uno.{module_name}", module_path)
                    if spec is None or spec.loader is None:
                        continue
                    test_module = importlib.util.module_from_spec(spec)
                    sys.modules[f"plugin.tests.uno.{module_name}"] = test_module
                    spec.loader.exec_module(test_module)

                    doc_to_pass = None
                    if "writer" in module_name or "format" in module_name:
                        # Writer core tests mutate the document and assume an empty starting state,
                        # so we pass None to force it to create its own hidden temporary document.
                        if "test_writer" not in module_name:
                            doc_to_pass = writer_doc
                    elif "calc" in module_name:
                        doc_to_pass = calc_doc
                    elif "draw" in module_name or "impress" in module_name:
                        doc_to_pass = draw_doc

                    _run_suite(ctx, suites, f"uno.{module_name}", test_module, doc_to_pass)
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

    for suite in suites:
        total_passed += int(suite.get("passed", 0) or 0)
        total_failed += int(suite.get("failed", 0) or 0)

    summary: Dict[str, Any] = {
        "total_passed": total_passed,
        "total_failed": total_failed,
        "suites": suites,
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


def main() -> int:
    """Command-line entrypoint: bootstrap LO and run tests.

    This lets you run tests from a normal shell without clicking menus:

        python -m plugin.testing_runner

    The import of officehelper/uno is done lazily so that this module
    can still be imported inside LibreOffice without pulling them in.
    """
    try:
        import officehelper  # type: ignore[import]
    except ImportError:
        print("ERROR: officehelper module is not available; run with LibreOffice's Python.", flush=True)
        return 1

    ctx = officehelper.bootstrap()
    if ctx is None:
        print("ERROR: Could not bootstrap LibreOffice (officehelper.bootstrap() returned None).", flush=True)
        return 1

    summary_json = run_all_tests(ctx)
    print(summary_json, flush=True)

    try:
        summary = json.loads(summary_json)
    except Exception:
        summary = {"total_failed": 1}

    # Force-close LibreOffice so it doesn't stay running (and mess up the screen).
    try:
        desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        if desktop:
            # Close all open components without saving (to avoid blocking termination)
            try:
                comps = desktop.getComponents().createEnumeration()
                while comps.hasMoreElements():
                    c = comps.nextElement()
                    if hasattr(c, "close"):
                        try:
                            c.close(True)
                        except Exception:
                            pass
            except Exception:
                pass
            desktop.terminate()
    except Exception:
        pass

    # Print a compact "tail" summary so callers can scan results quickly
    # even when the output above includes verbose tracebacks/log spam.
    total_passed = int(summary.get("total_passed", 0) or 0)
    total_failed = int(summary.get("total_failed", 0) or 0)
    print(f'"total_passed": {total_passed},', flush=True)
    print(f'"total_failed": {total_failed},', flush=True)

    return 0 if int(summary.get("total_failed", 0) or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

