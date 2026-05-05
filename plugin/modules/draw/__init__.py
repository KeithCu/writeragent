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
"""Draw module — tools for Draw/Impress document manipulation."""

from plugin.framework.module_base import ModuleBase

# Import submodules to ensure tools are registered via auto_discover_package
from plugin.modules.draw import specialized as specialized
from plugin.modules.draw import tree as tree
from plugin.modules.draw import headers_footers as headers_footers


class DrawModule(ModuleBase):
    """Registers Draw/Impress tools for shapes, pages/slides."""

    def initialize(self, services):
        self.services = services

        services.tools.auto_discover_package(__name__)
