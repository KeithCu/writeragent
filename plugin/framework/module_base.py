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
"""Base class for all modules."""

import logging
from abc import ABC

log = logging.getLogger("writeragent.module_base")


class ModuleBase(ABC):
    """Base class for all WriterAgent modules.

    Modules declare their manifest in module.yaml (config, requires,
    provides_services). This class handles the runtime behavior:
    initialization, event wiring, and shutdown.

    The ``name`` attribute is set automatically from _manifest.py at load
    time — it does NOT need to be set in the subclass.
    """

    name: str | None = None

    def initialize(self, services):
        """Phase 1: Called in dependency order during bootstrap.

        Use this to register services, wire event subscriptions, and
        create internal objects. All core services are available.

        Args:
            services: ServiceRegistry with attribute access to all
                      registered services (services.config, services.events …).
        """

    def start(self, services):
        """Phase 2a: Called on the VCL main thread after ALL modules
        have initialized.

        Safe for UNO operations: document listeners, UI setup, toolkit
        calls. Dispatched via QueueExecutor.execute() (blocking).
        Called in dependency order.

        Args:
            services: ServiceRegistry with attribute access to all
                      registered services.
        """

    def start_background(self, services):
        """Phase 2b: Called on the Job thread after all start() complete.

        Launch background tasks: HTTP servers, LLM connections, polling.
        Called in dependency order.

        Args:
            services: ServiceRegistry with attribute access to all
                      registered services.
        """

    def shutdown(self):
        """Stop background tasks, close connections.

        Called in reverse dependency order on extension unload."""

    # ── Action dispatch ──────────────────────────────────────────────

    def on_action(self, action):
        """Handle an action dispatched from menu/shortcut. Override in subclass."""
        log.warning("Unhandled action '%s' on module '%s'", action, self.name)

    def get_menu_text(self, action):
        """Return dynamic menu text for an action, or None for default.

        Override in subclass to provide state-dependent menu labels.
        Return None to keep the static title from module.yaml.
        """
        return None

    def get_menu_icon(self, action):
        """Return dynamic icon name prefix for an action, or None for default.

        Override in subclass to provide state-dependent menu icons.
        Return an icon prefix like "running", "stopped", "starting".
        The framework will load ``{prefix}_16.png`` from ``extension/icons/``.
        Return None to keep the icon declared in module.yaml.
        """
        return None

    # ── Dialog helpers ───────────────────────────────────────────────

    def load_dialog(self, dialog_name):
        """Load an XDL dialog from this module's directory."""
        from plugin.framework.dialogs import load_module_dialog
        return load_module_dialog(self.name, dialog_name)

    def load_framework_dialog(self, dialog_name):
        """Load a reusable framework dialog template."""
        from plugin.framework.dialogs import load_framework_dialog
        return load_framework_dialog(dialog_name)
