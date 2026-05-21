"""The main API for the v4 notebook format."""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.

# --- WriterAgent vendored from nbformat v5.10.4 ---
# Disabled: v1/v2/v3, traitlets, fastjsonschema validation (see README.md)
from __future__ import annotations

__all__ = [
    # "downgrade",  # WriterAgent: v4/convert.py not shipped (traitlets)
    # "nbformat",
    # "nbformat_minor",
    # "nbformat_schema",
    # "new_code_cell",
    # "new_markdown_cell",
    # "new_notebook",
    # "new_output",
    # "new_raw_cell",
    # "output_from_msg",
    "reads",
    "to_notebook",
    # "upgrade",
    "writes",
]

# from .convert import downgrade, upgrade  # WriterAgent: traitlets + validator
# from .nbbase import (  # WriterAgent: nbformat.corpus + validate not shipped
#     nbformat,
#     nbformat_minor,
#     nbformat_schema,
#     new_code_cell,
#     new_markdown_cell,
#     new_notebook,
#     new_output,
#     new_raw_cell,
#     output_from_msg,
# )
from .nbjson import reads, to_notebook, writes

reads_json = reads
writes_json = writes
to_notebook_json = to_notebook
