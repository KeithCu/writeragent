"""Read Jupyter notebooks (.ipynb) as NotebookNode trees — nbformat v4 only."""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
# WriterAgent: vendored subset — see README.md in this directory.

from __future__ import annotations

from .notebooknode import NotebookNode, from_dict
from .reader import NBFormatError, NotJSONError, get_version, parse_json, read, reads
from .v4.rwbase import rejoin_lines, split_lines, strip_transient
from .v4.nbjson import to_notebook, writes


def read_ipynb(path: str, *, encoding: str = "utf-8") -> NotebookNode:
    """Load a .ipynb file from disk (v4 only)."""
    with open(path, encoding=encoding) as f:
        return read(f)


__all__ = [
    "NBFormatError",
    "NotJSONError",
    "NotebookNode",
    "from_dict",
    "get_version",
    "parse_json",
    "read",
    "read_ipynb",
    "reads",
    "rejoin_lines",
    "split_lines",
    "strip_transient",
    "to_notebook",
    "writes",
]
