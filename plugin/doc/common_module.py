# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""WriterAgent chat tool discovery for all document types.

Not shipped in LibrePy. ``plugin/doc/__init__.py`` re-exports ``CommonModule``
when this file is present so ``load_modules`` still finds it on ``plugin.doc``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from plugin.framework.module_base import ModuleBase

_TOOL_MODULES = (
    "plugin.doc.diagnostics",
    "plugin.doc.document_research_tools",
    "plugin.doc.document_research_grep_tool",
    "plugin.embeddings.document_research_fts_tool",
    "plugin.doc.document_research_specialized",
    "plugin.doc.find_tools_tool",
    "plugin.doc.python_domain_specialized",
    "plugin.doc.peer_message",
    "plugin.doc.print_doc",
    "plugin.doc.undo",
)


class CommonModule(ModuleBase):
    """Provides generic document tools (info, save, export)."""

    def initialize(self, services: Any) -> None:
        for name in _TOOL_MODULES:
            services.tools.auto_discover(import_module(name))
