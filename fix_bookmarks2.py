with open('plugin/writer/specialized/bookmarks.py', 'r') as f:
    content = f.read()

old_block = """            for name in names:
                if not name.startswith("_mcp_"):
                    continue
                bm = bookmarks.getByName(name)
                anchor = bm.getAnchor()
                para_idx = find_paragraph_for_range(anchor, para_ranges, text_obj)
                if para_idx >= 0:
                    result[para_idx] = name"""

new_block = """            from plugin.writer.tree import confirm_paragraph_index
            for name in names:
                if not name.startswith("_mcp_"):
                    continue
                bm = bookmarks.getByName(name)
                try:
                    anchor = bm.getAnchor()
                except Exception:
                    continue
                if anchor is None:
                    continue
                para_idx = find_paragraph_for_range(anchor, para_ranges, text_obj)
                placed = confirm_paragraph_index(text_obj, anchor, para_ranges, para_idx)
                if placed is not None:
                    result[placed] = name"""

content = content.replace(old_block, new_block)

with open('plugin/writer/specialized/bookmarks.py', 'w') as f:
    f.write(content)
