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

import json
import traceback
from typing import Any, Dict, List

from plugin.framework.uno_helpers import get_active_document


def test(func):
    """Decorator to mark a function as a test in the native test runner."""
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

def _run_suite(
    ctx: Any,
    suites: List[Dict[str, Any]],
    name: str,
    module,
    *args,
) -> None:
    """Run a test module using the decorator-based native runner.

    Collects functions marked with @setup, @teardown, and @test.
    Executes setup(ctx), then all tests(ctx), then teardown(ctx).
    """
    total_passed = 0
    total_failed = 0
    log = []

    setup_func = None
    teardown_func = None
    test_funcs = []

    # Discover decorators, iterating over module dict to preserve insertion (definition) order
    for attr_name, attr in module.__dict__.items():
        if callable(attr):
            if getattr(attr, "_is_setup", False):
                setup_func = attr
            elif getattr(attr, "_is_teardown", False):
                teardown_func = attr
            elif getattr(attr, "_is_test", False):
                test_funcs.append(attr)

    # Note: For backwards compatibility, if we didn't find any @test functions,
    # check if there's a traditional monolithic test function
    if not test_funcs:
        # Fallback to the old run_*_tests approach for modules not yet migrated
        fallback_func_name = f"run_{name.split('.')[-1].replace('_tests', '').replace('test_', '')}_tests"
        if name == "calc.tests":
            fallback_func_name = "run_calc_tests"
        if name == "draw.tests":
            fallback_func_name = "run_draw_tests"

        fallback_func = getattr(module, fallback_func_name, None)
        if fallback_func:
            try:
                passed, failed, result_log = fallback_func(ctx, *args)
                suites.append({
                    "name": name,
                    "passed": int(passed or 0),
                    "failed": int(failed or 0),
                    "log": list(result_log or []),
                })
            except Exception as e:
                suites.append({
                    "name": name,
                    "passed": 0,
                    "failed": 1,
                    "log": [f"EXCEPTION: {e}", traceback.format_exc()],
                })
            return

    try:
        if setup_func:
            log.append(f"Running setup: {setup_func.__name__}")
            setup_func(ctx)

        for test_func in test_funcs:
            try:
                log.append(f"Running test: {test_func.__name__}")
                test_func()
                total_passed += 1
                log.append(f"OK: {test_func.__name__}")
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
                log.append(f"Running teardown: {teardown_func.__name__}")
                teardown_func(ctx)
            except Exception as e:
                total_failed += 1
                log.append(f"TEARDOWN EXCEPTION: {e}")
                log.append(traceback.format_exc())

    suites.append({
        "name": name,
        "passed": total_passed,
        "failed": total_failed,
        "log": log,
    })


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
    model = get_active_document(ctx)

    # Framework tests
    try:
        import plugin.tests.core_tests as core_tests
        _run_suite(ctx, suites, "framework.core_tests", core_tests)
    except ImportError:
        pass

    # Writer markdown / format-preserving tests
    try:
        from plugin.framework.document import is_writer  # local import to avoid hard dependency if unused
        import plugin.tests.format_tests as format_tests

        writer_doc = model if (model is not None and is_writer(model)) else None
        _run_suite(ctx, suites, "writer.format_tests", format_tests, writer_doc)
    except ImportError:
        # Suite not available in this build; skip silently.
        pass

    # Writer core / navigation tests
    try:
        from plugin.framework.document import is_writer  # local import
        import plugin.tests.test_writer as test_writer

        # Writer core tests mutate the document and assume an empty starting state,
        # so we pass None to force it to create its own hidden temporary document.
        _run_suite(ctx, suites, "writer.core_tests", test_writer)
    except ImportError:
        pass

    # Calc API / tool tests
    try:
        from plugin.framework.document import is_calc  # local import
        import plugin.tests.test_calc as test_calc

        calc_doc = model if (model is not None and is_calc(model)) else None
        _run_suite(ctx, suites, "calc.tests", test_calc, calc_doc)
    except ImportError:
        pass

    # Draw / Impress tests
    try:
        from plugin.framework.document import is_draw  # local import
        import plugin.tests.test_draw as test_draw

        draw_doc = model if (model is not None and is_draw(model)) else None
        _run_suite(ctx, suites, "draw.tests", test_draw, draw_doc)
    except ImportError:
        pass

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

    return 0 if int(summary.get("total_failed", 0) or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

