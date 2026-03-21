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
"""Base provider class for launcher CLI providers."""

import os
from abc import ABC
from plugin.framework.uno_context import get_active_document
from plugin.framework.document import get_document_path


class BaseProvider(ABC):
    """Base class for AI CLI providers."""

    name = ""
    label = ""
    binary_name = ""
    install_url = ""

    def __init__(self, services):
        self.services = services

    @property
    def config(self):
        return self.services.get("config")

    def get_args(self, mcp_url):
        """Return a list of CLI arguments."""
        return []

    def setup_env(self, cwd, mcp_url):
        """Perform provider-specific environment setup."""
        from .. import write_unified_prompt
        write_unified_prompt(cwd, self.name)
        return {}

    def get_default_cwd(self):
        """Return the default working directory."""
        try:
            model = get_active_document()
            if model:
                p = get_document_path(model)
                if p and os.path.isfile(p):
                    return os.path.dirname(p)
        except Exception:
            pass
        return os.path.expanduser("~")
