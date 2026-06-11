# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Common tools for all document types."""

from plugin.framework.module_base import ModuleBase


class CommonModule(ModuleBase):
    """Provides generic document tools (info, save, export)."""

    def initialize(self, services):
        self.services = services

        from . import (
            diagnostics,
            document_research_fts_tool,
            document_research_grep_tool,
            document_research_search_tool,
            document_research_specialized,
            document_research_tools,
            print_doc,
            undo,
        )

        discovery_modules = (
            diagnostics,
            document_research_tools,
            document_research_grep_tool,
            document_research_fts_tool,
            document_research_search_tool,
            document_research_specialized,
            print_doc,
            undo,
        )

        for module in discovery_modules:
            services.tools.auto_discover(module)
