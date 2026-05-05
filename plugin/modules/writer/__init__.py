# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Writer module — tools for Writer document manipulation."""

from plugin.framework.module_base import ModuleBase

from . import bookmarks, tree, proximity, index
from . import base as base, specialized as specialized, styles as styles, shapes as shapes, charts as charts, bookmark_tools as bookmark_tools, indexes as indexes, fields as fields, footnotes as footnotes, embedded as embedded, tracking as tracking, forms as forms


class WriterModule(ModuleBase):
    """Registers Writer tools for outline, content, comments, styles, etc."""

    def initialize(self, services):
        self.services = services

        # Initialize core Writer services via auto-discovery

        # Order matters: tree needs bookmarks, proximity/index need tree
        for module in (bookmarks, tree, proximity, index):
            services.auto_discover(module)

        # Register tools automatically for the entire package
        services.tools.auto_discover_package(__name__)
