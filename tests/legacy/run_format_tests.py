"""
Markdown support tests — run from within LibreOffice (WriterAgent menu: Run markdown tests).
The test runner lives in markdown_support.run_markdown_tests so the menu works without
packaging the tests/ directory. This module re-exports it for local/source runs.
"""

import sys
import os

if __name__ == "__main__":
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)

from plugin.framework.format_tests import run_markdown_tests

__all__ = ["run_markdown_tests"]
