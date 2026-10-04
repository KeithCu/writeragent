with open('plugin/writer/proximity.py', 'r') as f:
    content = f.read()

old_init = """        self._flat_cache: dict[Any, list[Any]] = {}  # doc_key -> [flat entries]
        self._flat_fp: dict[Any, int | None] = {}  # doc_key -> CharacterCount at flatten time"""

new_init = """        self._flat_cache: dict[Any, list[Any]] = {}  # doc_key -> [flat entries]
        self._flat_fp: dict[Any, int | None] = {}  # doc_key -> CharacterCount at flatten time
        self._flat_root: dict[Any, dict[str, Any]] = {}  # doc_key -> tree object that was flattened"""

content = content.replace(old_init, new_init)

old_drop = """    def _drop_flat_cache(self, key: Any | None = None) -> None:
        if key is None:
            self._flat_cache.clear()
            self._flat_fp.clear()
            return
        self._flat_cache.pop(key, None)
        self._flat_fp.pop(key, None)"""

new_drop = """    def _drop_flat_cache(self, key: Any | None = None) -> None:
        if key is None:
            self._flat_cache.clear()
            self._flat_fp.clear()
            self._flat_root.clear()
            return
        self._flat_cache.pop(key, None)
        self._flat_fp.pop(key, None)
        self._flat_root.pop(key, None)"""

content = content.replace(old_drop, new_drop)


old_flatten = """    def _flatten_tree(self, root: dict[str, Any], doc: Any) -> list[Any]:
        key = self._doc_svc.doc_key(doc)
        fingerprint = _heading_tree_fingerprint(doc)
        if is_cacheable_doc_key(key) and key in self._flat_cache:
            cached_fp = self._flat_fp.get(key)
            if fingerprint is None or fingerprint == cached_fp:
                return self._flat_cache[key]

        flat: list[Any] = []
        self._flatten_recurse(root["children"], None, flat)
        if is_cacheable_doc_key(key):
            self._flat_cache[key] = flat
            self._flat_fp[key] = fingerprint
        return flat"""

new_flatten = """    def _flatten_tree(self, root: dict[str, Any], doc: Any) -> list[Any]:
        key = self._doc_svc.doc_key(doc)
        fingerprint = _heading_tree_fingerprint(doc)
        if is_cacheable_doc_key(key) and key in self._flat_cache:
            cached_fp = self._flat_fp.get(key)
            # What was wrong: any cache hit returned the previous flat list and
            # ignored ``root``. A programmatic edit that XModifyListener misses
            # still moves CharacterCount, so TreeService rebuilds and hands the
            # new tree here. next/previous/sibling/parent kept walking the old
            # paragraphs.
            # Why: same CharacterCount rule as TreeService.build_heading_tree
            # (no count → listener-only). Also require this tree object: a
            # rebuilt root must not reuse the list flattened from the previous one.
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
