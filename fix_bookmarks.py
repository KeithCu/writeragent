with open('plugin/writer/specialized/bookmarks.py', 'r') as f:
    content = f.read()

old_block = """        if needs_bookmark:
            with self._untracked(doc):
                for para_idx, start_range in needs_bookmark:
                    bm_name = "_mcp_%s" % uuid.uuid4().hex[:8]
                    if self._insert_named_bookmark(doc, text, bm_name, start_range):
                        bookmark_map[para_idx] = bm_name

        # No doc.store(): this runs inside READ tools. Locators stay in"""

new_block = """        kept = set(bookmark_map.values())
        stale = self._demoted_mcp_names(doc, kept) if _SAVE_HOOK_DEPTH == 0 else []

        if needs_bookmark or stale:
            with self._untracked(doc):
                for para_idx, start_range in needs_bookmark:
                    bm_name = "_mcp_%s" % uuid.uuid4().hex[:8]
                    if self._insert_named_bookmark(doc, text, bm_name, start_range):
                        bookmark_map[para_idx] = bm_name
                if stale:
                    self._remove_named_bookmarks(doc, stale)

        # No doc.store(): this runs inside READ tools. Locators stay in"""

content = content.replace(old_block, new_block)

helpers_block = """    def cleanup_mcp_bookmarks(self, doc: Any) -> int:"""

new_helpers = """    def _demoted_mcp_names(self, doc: Any, kept: set[str]) -> list[str]:
        \"\"\"``_mcp_`` names that are not on a current heading.\"\"\"
        if not hasattr(doc, "getBookmarks"):
            return []
        try:
            names = doc.getBookmarks().getElementNames()
        except Exception:
            log.exception("Failed to list heading bookmarks for prune")
            return []
        stale: list[str] = []
        for name in names:
            if isinstance(name, str) and name.startswith("_mcp_") and name not in kept:
                stale.append(name)
        return stale

    def _remove_named_bookmarks(self, doc: Any, names: list[str]) -> None:
        \"\"\"Remove these bookmarks. Caller holds ``_untracked``.\"\"\"
        if not names or not hasattr(doc, "getBookmarks"):
            return
        bookmarks = doc.getBookmarks()
        text = doc.getText()
        for name in names:
            try:
                bm = bookmarks.getByName(name)
                text.removeTextContent(bm)
            except Exception:
                log.debug("Failed to prune stale heading bookmark %s", name, exc_info=True)

    def cleanup_mcp_bookmarks(self, doc: Any) -> int:"""

content = content.replace(helpers_block, new_helpers)

with open('plugin/writer/specialized/bookmarks.py', 'w') as f:
    f.write(content)
