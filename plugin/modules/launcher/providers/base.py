"""Base provider class for launcher CLI providers."""

import os
from abc import ABC

from plugin.framework.uno_context import get_ctx
from plugin.modules.core.services.document import get_document_path


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
        ctx = get_ctx()
        if not ctx:
            return os.path.expanduser("~")

        try:
            desk = ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.frame.Desktop", ctx
            )
            model = desk.getCurrentComponent()
            if model:
                p = get_document_path(model)
                if p and os.path.isfile(p):
                    return os.path.dirname(p)
        except Exception:
            pass
        return os.path.expanduser("~")
