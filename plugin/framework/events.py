"""EventBusService — wraps the framework EventBus as a named service."""

from plugin.framework.event_bus import EventBus, global_event_bus
from plugin.framework.service_base import ServiceBase


class EventBusService(ServiceBase, EventBus):
    """Singleton event bus exposed as a service.

    Inherits from both ServiceBase (for registry) and EventBus (for
    pub/sub). Modules access it as ``services.events``.
    """

    name = "events"

    def __init__(self):
        ServiceBase.__init__(self)
        self._subscribers = global_event_bus._subscribers
