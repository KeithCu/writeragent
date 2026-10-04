with open("plugin/embeddings/document_research_fts_tool.py", "r") as f:
    text = f.read()

import re

search_block = """\
        try:
            result = hybrid_search(
                ctx.ctx,
                search_path,
                str(query),
                k,
                model=model,
                near_slop=near_slop,
            )
"""
replace_block = """\
        try:
            result = hybrid_search(
                ctx.ctx,
                search_path,
                str(query),
                k,
                model=model,
                near_slop=near_slop,
                doc_url_filter=context_result["allowed_urls"],
            )
"""
if search_block in text:
    text = text.replace(search_block, replace_block)
    with open("plugin/embeddings/document_research_fts_tool.py", "w") as f:
        f.write(text)
    print("Replaced successfully (part 2, old block)")
else:
    # already replaced?
    if "doc_url_filter=context_result[\"allowed_urls\"]" in text:
        print("Already replaced!")

search_block_hits = """\
        hits = list(result.get("hits") or [])
        if allowed_urls is not None:
            hits = [h for h in hits if h.get("doc_url") in allowed_urls]

        return {
"""
replace_block_hits = """\
        hits = list(result.get("hits") or [])
        return {
"""

if search_block_hits in text:
    text = text.replace(search_block_hits, replace_block_hits)
    with open("plugin/embeddings/document_research_fts_tool.py", "w") as f:
        f.write(text)
    print("Replaced hits filtering block successfully")
