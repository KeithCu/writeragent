with open('plugin/writer/proximity.py', 'r') as f:
    content = f.read()

old_init = """    def __init__(self, services: Any) -> None:
        self._doc_svc = services.document
        self._tree_svc = services.writer_tree
        self._bm_svc = services.writer_bookmarks
        events = services.events
        self._flat_cache: dict[Any, list[Any]] = {}  # doc_key -> [flat entries]
        events.subscribe("document:cache_invalidated", self._on_cache_invalidated)"""

new_init = """    def __init__(self, services: Any) -> None:
        self._doc_svc = services.document
        self._tree_svc = services.writer_tree
        self._bm_svc = services.writer_bookmarks
        events = services.events
        self._flat_cache: dict[Any, list[Any]] = {}  # doc_key -> [flat entries]
        self._flat_fp: dict[Any, int | None] = {}
        self._flat_root: dict[Any, dict[str, Any]] = {}
        events.subscribe("document:cache_invalidated", self._on_cache_invalidated)"""

content = content.replace(old_init, new_init)

old_drop = """    def _on_cache_invalidated(self, doc: Any | None = None, key: Any | None = None, **_kw: Any) -> None:
        # key= first: close/unload emits the stored key without a live model.
        if key is not None:
            self._flat_cache.pop(key, None)
        elif doc is None:
            self._flat_cache.clear()
        else:
            self._flat_cache.pop(self._doc_svc.doc_key(doc), None)"""

new_drop = """    def _on_cache_invalidated(self, doc: Any | None = None, key: Any | None = None, **_kw: Any) -> None:
        # key= first: close/unload emits the stored key without a live model.
        if key is not None:
            self._flat_cache.pop(key, None)
            self._flat_fp.pop(key, None)
            self._flat_root.pop(key, None)
        elif doc is None:
            self._flat_cache.clear()
            self._flat_fp.clear()
            self._flat_root.clear()
        else:
            k = self._doc_svc.doc_key(doc)
            self._flat_cache.pop(k, None)
            self._flat_fp.pop(k, None)
            self._flat_root.pop(k, None)"""

content = content.replace(old_drop, new_drop)


old_flatten = """    def _flatten_tree(self, root: dict[str, Any], doc: Any) -> list[Any]:
        key = self._doc_svc.doc_key(doc)
        if is_cacheable_doc_key(key) and key in self._flat_cache:
            return self._flat_cache[key]

        flat: list[Any] = []
        self._flatten_recurse(root["children"], None, flat)
        if is_cacheable_doc_key(key):
            self._flat_cache[key] = flat
        return flat"""

new_flatten = """    def _flatten_tree(self, root: dict[str, Any], doc: Any) -> list[Any]:
        from plugin.writer.tree import _heading_tree_fingerprint
        key = self._doc_svc.doc_key(doc)
        fingerprint = _heading_tree_fingerprint(doc)
        if is_cacheable_doc_key(key) and key in self._flat_cache:
            cached_fp = self._flat_fp.get(key)
            if (fingerprint is None or fingerprint == cached_fp) and self._flat_root.get(key) is root:
                return self._flat_cache[key]

        flat: list[Any] = []
        self._flatten_recurse(root["children"], None, flat)
        if is_cacheable_doc_key(key):
            self._flat_cache[key] = flat
            self._flat_fp[key] = fingerprint
            self._flat_root[key] = root
        return flat"""

content = content.replace(old_flatten, new_flatten)

with open('plugin/writer/proximity.py', 'w') as f:
    f.write(content)
