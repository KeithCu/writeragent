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
"""Base class for all services."""

from abc import ABC


class ServiceBase(ABC):
    """Abstract base for services registered in the ServiceRegistry.

    Services provide horizontal capabilities (document manipulation,
    config access, LLM streaming, etc.) that modules and tools consume.

    Attributes:
        name: Unique service identifier (e.g. "document", "config").
    """

    name: str | None = None

    def initialize(self, ctx):
        """Called once during bootstrap with the UNO component context.

        Override to perform setup that requires UNO (desktop access,
        service manager, etc.).

        Args:
            ctx: UNO component context (com.sun.star.uno.XComponentContext).
        """

    def shutdown(self):
        """Called on extension unload. Override to clean up."""
