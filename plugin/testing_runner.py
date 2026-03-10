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


def _run_suite(
    ctx: Any,
    suites: List[Dict[str, Any]],
    name: str,
    func,
    *func_args,
) -> None:
    """Run a single test suite function and append its result to suites.

    The suite function is expected to return (passed_count, failed_count, log_list).
    Any unexpected exception is converted into a single failed test with traceback.
    """
    total_passed = 0
    total_failed = 0

    try:
        passed, failed, log = func(ctx, *func_args)
        total_passed += int(passed or 0)
        total_failed += int(failed or 0)
        suites.append(
            {
                "name": name,
                "passed": total_passed,
                "failed": total_failed,
                "log": list(log or []),
            }
        )
    except Exception as e:  # noqa: BLE001
        # Treat any unexpected exception as a single failed test in this suite.
        total_failed += 1
        suites.append(
            {
                "name": name,
                "passed": 0,
                "failed": total_failed,
                "log": [
                    "EXCEPTION: %s" % (e,),
                    traceback.format_exc(),
                ],
            }
        )


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
        from plugin.framework.core_tests import run_framework_tests
        _run_suite(ctx, suites, "framework.core_tests", run_framework_tests)
    except ImportError:
        pass

    # Writer markdown / format-preserving tests
    try:
        from plugin.framework.document import is_writer  # local import to avoid hard dependency if unused
        from plugin.framework.format_tests import run_markdown_tests

        writer_doc = model if (model is not None and is_writer(model)) else None
        _run_suite(ctx, suites, "writer.format_tests", run_markdown_tests, writer_doc)
    except ImportError:
        # Suite not available in this build; skip silently.
        pass

    # Writer core / navigation tests
    try:
        from plugin.framework.document import is_writer  # local import
        from plugin.modules.writer.tests import run_writer_tests

        # Writer core tests mutate the document and assume an empty starting state,
        # so we pass None to force it to create its own hidden temporary document.
        _run_suite(ctx, suites, "writer.core_tests", run_writer_tests)
    except ImportError:
        pass

    # Calc API / tool tests
    try:
        from plugin.framework.document import is_calc  # local import
        from plugin.modules.calc.tests import run_calc_tests

        calc_doc = model if (model is not None and is_calc(model)) else None
        _run_suite(ctx, suites, "calc.tests", run_calc_tests, calc_doc)
    except ImportError:
        pass

    # Future: add Draw/Impress or other suites here as they are implemented.

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

