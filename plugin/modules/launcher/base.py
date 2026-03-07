"""Base provider class for launcher CLI providers."""

import os
from abc import ABC
import uno
import unohelper
from plugin.framework.uno_helpers import get_desktop, get_active_document
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
