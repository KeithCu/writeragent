"""
Format support tests — run from within LibreOffice (WriterAgent menu: Run Format tests).
The test runner lives in plugin.tests.format_tests.run_format_tests. This module re-exports
it for local/source runs (e.g. python -m plugin.tests.run_format_tests).
"""

import sys
from plugin.framework.utils import get_plugin_dir
import os

if __name__ == "__main__":
    # Repo root (parent of plugin/) so that "plugin" package is importable
    _root = os.path.dirname(get_plugin_dir())
    if _root not in sys.path:
        sys.path.insert(0, _root)

from plugin.testing_runner import run_module_suite
from plugin.tests.uno import format_tests

def run_format_tests(ctx, doc_model=None):
    return run_module_suite(ctx, format_tests, "writer.format_tests", doc_model)

__all__ = ["run_format_tests"]
