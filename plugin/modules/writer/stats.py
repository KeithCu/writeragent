import json
from plugin.modules.core.services.document import get_document_length, get_paragraph_ranges

def _err(message):
    return json.dumps({"status": "error", "message": message})

def tool_get_document_stats(model, ctx, args):
    """Return document statistics (length, paragraphs, pages)."""
    try:
        length = get_document_length(model)
        paras = len(get_paragraph_ranges(model))
        pages = 0
        try:
            vc = model.getCurrentController().getViewCursor()
            # Jump to end to get actual page count
            old_pos = vc.getStart()
            vc.gotoEnd(False)
            pages = vc.getPage()
            vc.gotoRange(old_pos, False)
        except Exception:
            pass
        return json.dumps({
            "status": "ok",
            "character_count": length,
            "paragraph_count": paras,
            "page_count": pages
        })
    except Exception as e:
        return _err(str(e))

STATS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_document_stats",
            "description": "Get document statistics: character count, paragraph count, and total pages.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
    },
]
