
with open("plugin/writer/content.py", "r") as f:
    text = f.read()

content_replacement_2 = """            if doomed and count == 0:
                resp = _table_deletion_result([name for _table, name in doomed], tracked_delete)
            else:
                resp = search_mod.build_search_replace_response(count, use_preserve=use_preserve)
                from plugin.writer.search import _MAX_SEARCH_REPLACEMENTS
                if max_limit_hit:
                    resp["message"] += f" Note: Replacement stopped at the safety limit of {_MAX_SEARCH_REPLACEMENTS} to avoid excessive document changes. The document is not fully updated."
                if count > 1:"""
text = re.sub(r'            if doomed and count == 0:\n                resp = _table_deletion_result\(\[name for _table, name in doomed\], tracked_delete\)\n            else:\n                msg = None\n                from plugin\.writer\.search import _MAX_SEARCH_REPLACEMENTS\n                if max_limit_hit:\n                    msg = f"Replaced {count} occurrence\(s\)\. Note: Replacement stopped at the safety limit of {_MAX_SEARCH_REPLACEMENTS} to avoid excessive document changes\. The document is not fully updated\."\n                resp = search_mod\.build_search_replace_response\(count, message=msg, use_preserve=use_preserve\)\n                if count > 1:', content_replacement_2, text)

with open("plugin/writer/content.py", "w") as f:
    f.write(text)
