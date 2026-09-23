# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""WriterAgent chat tool discovery for all document types.

Not shipped in LibrePy. ``plugin/doc/__init__.py`` re-exports ``CommonModule``
when this file is present so ``load_modules`` still finds it on ``plugin.doc``.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.module_base import ModuleBase


class CommonModule(ModuleBase):
    """Provides generic document tools (info, save, export)."""

    services: Any

    def initialize(self, services: Any) -> None:
        self.services = services

        from . import (
            diagnostics,
            document_research_grep_tool,
            document_research_specialized,
            document_research_tools,
            find_tools_tool,
            peer_message,
            print_doc,
            undo,
        )
        from plugin.embeddings import (
            document_research_fts_tool,
        )

        discovery_modules = (
            diagnostics,
            document_research_tools,
            document_research_grep_tool,
            document_research_fts_tool,
            document_research_specialized,
            find_tools_tool,
            peer_message,
            print_doc,
            undo,
        )

        for module in discovery_modules:
            services.tools.auto_discover(module)
