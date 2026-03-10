"""
Markdown support tests — run from within LibreOffice (WriterAgent menu: Run markdown tests).
The test runner lives in plugin.tests.format_tests.run_markdown_tests. This module re-exports
it for local/source runs (e.g. python -m plugin.tests.run_format_tests).
"""

import sys
import os

if __name__ == "__main__":
    # Repo root (parent of plugin/) so that "plugin" package is importable
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _root not in sys.path:
        sys.path.insert(0, _root)

from plugin.tests.format_tests import run_markdown_tests

__all__ = ["run_markdown_tests"]
