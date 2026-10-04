with open('plugin/doc/diagnostics.py', 'r') as f:
    content = f.read()

old_block = """                        if anchor is None or not anchor.getString():
                            issues.append({"type": "broken_bookmark", "severity": "warning", "paragraph_index": -1, "message": ("Bookmark '%s' has an empty anchor." % name), "detail": ("Bookmark '%s' has an empty anchor." % name)})"""

new_block = """                        if anchor is None:
                            issues.append({"type": "broken_bookmark", "severity": "warning", "paragraph_index": -1, "message": ("Bookmark '%s' has an empty anchor." % name), "detail": ("Bookmark '%s' has an empty anchor." % name)})
                            continue
                        # What was wrong: `not anchor.getString()` flagged every
                        # point bookmark. Heading `_mcp_` marks are inserted as
                        # a collapsed cursor, so their anchor string is empty
                        # and document_health_check reported each one as broken.
                        # Why: an empty string with a start position is a point
                        # bookmark. Broken means no anchor, or an anchor whose
                        # text and start both cannot be read.
                        try:
                            anchor_text = anchor.getString()
                        except Exception:
                            anchor_text = None
                        if anchor_text:
                            continue
                        get_start = getattr(anchor, "getStart", None)
                        if get_start is None:
                            issues.append({"type": "broken_bookmark", "severity": "warning", "paragraph_index": -1, "message": ("Bookmark '%s' has an empty anchor." % name), "detail": ("Bookmark '%s' has an empty anchor." % name)})
                            continue
                        try:
                            get_start()
                        except Exception:
                            issues.append({"type": "broken_bookmark", "severity": "warning", "paragraph_index": -1, "message": ("Bookmark '%s' has an empty anchor." % name), "detail": ("Bookmark '%s' has an empty anchor." % name)})
"""

content = content.replace(old_block, new_block)

with open('plugin/doc/diagnostics.py', 'w') as f:
    f.write(content)
