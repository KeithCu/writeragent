with open('plugin/writer/specialized/bookmarks.py', 'r') as f:
    content = f.read()

old_block = """        bm = bookmarks.getByName(bookmark_name)
        anchor = bm.getAnchor()

        # Find paragraph index
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        para_idx = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)"""

new_block = """        bm = bookmarks.getByName(bookmark_name)
        try:
            anchor = bm.getAnchor()
        except Exception:
            return self._tool_error(f"Bookmark '{bookmark_name}' anchor is not in the document.")

        if anchor is None:
            return self._tool_error(f"Bookmark '{bookmark_name}' anchor is not in the document.")

        # Find paragraph index
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        para_idx = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)

        from plugin.writer.tree import confirm_paragraph_index
        if confirm_paragraph_index(text_obj, anchor, para_ranges, para_idx) is None:
            return self._tool_error(f"Bookmark '{bookmark_name}' anchor is not in the document.")"""

content = content.replace(old_block, new_block)

with open('plugin/writer/specialized/bookmarks.py', 'w') as f:
    f.write(content)
