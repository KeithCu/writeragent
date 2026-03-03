"""Writer module — tools for Writer document manipulation."""

from plugin.framework.module_base import ModuleBase


class WriterModule(ModuleBase):
    """Registers Writer tools for outline, content, comments, styles, etc."""

    def initialize(self, services):
        self.services = services

        # Initialize core Writer services (merged from writer_nav and writer_index)
        from .services.bookmarks import BookmarkService
        from .services.tree import TreeService
        from .services.proximity import ProximityService
        from .services.index import IndexService

        doc_svc = services.document
        events = services.events

        bm = BookmarkService(doc_svc, events)
        tree = TreeService(doc_svc, bm, events)
        prox = ProximityService(doc_svc, tree, bm, events)
        idx = IndexService(doc_svc, tree, bm, events)

        services.register_instance("writer_bookmarks", bm)
        services.register_instance("writer_tree", tree)
        services.register_instance("writer_proximity", prox)
        services.register_instance("writer_index", idx)
