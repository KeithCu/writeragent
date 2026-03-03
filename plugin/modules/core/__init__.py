"""Core module — provides fundamental services."""

from plugin.framework.module_base import ModuleBase


class Module(ModuleBase):

    def initialize(self, services):
        from plugin.modules.core.services.document import DocumentService
        from plugin.modules.core.services.config import ConfigService
        from plugin.modules.core.services.events import EventBusService
        from plugin.modules.core.services.format import FormatService

        services.register(DocumentService())
        services.register(ConfigService())
        services.register(EventBusService())
        services.register(FormatService())
