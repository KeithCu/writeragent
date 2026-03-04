import json
from plugin.modules.core.services.document import get_paragraph_ranges

def _err(message):
    return json.dumps({"status": "error", "message": message})

def tool_read_paragraphs(model, ctx, args):
    """Read a range of paragraphs by index."""
    start = args.get("start_index", 0)
    count = args.get("count", 10)
    try:
        ranges = get_paragraph_ranges(model)
        end = min(start + count, len(ranges))
        paras = []
        for i in range(start, end):
            p = ranges[i]
            text = p.getString() if hasattr(p, "getString") else "[Object]"
            paras.append({"index": i, "text": text})
        return json.dumps({"status": "ok", "paragraphs": paras, "total": len(ranges)})
    except Exception as e:
        return _err(str(e))

def tool_insert_at_paragraph(model, ctx, args):
    """Insert text at a specific paragraph index."""
    para_index = args.get("paragraph_index")
    text_to_insert = args.get("text", "")
    position = args.get("position", "before") # before, after, replace
    
    if para_index is None:
        return _err("paragraph_index is required")
        
    try:
        ranges = get_paragraph_ranges(model)
        if para_index < 0 or para_index >= len(ranges):
            return _err(f"Paragraph index {para_index} out of range (0..{len(ranges)-1})")
            
        target_para = ranges[para_index]
        text = model.getText()
        cursor = text.createTextCursorByRange(target_para.getStart())
        
        if position == "after":
            cursor.gotoRange(target_para.getEnd(), False)
            text.insertString(cursor, "\\n" + text_to_insert, False)
        elif position == "replace":
            cursor.gotoRange(target_para.getEnd(), True)
            cursor.setString(text_to_insert)
        else: # before
            text.insertString(cursor, text_to_insert + "\\n", False)
            
        return json.dumps({"status": "ok", "message": f"Inserted text at paragraph {para_index}"})
    except Exception as e:
        return _err(str(e))

CONTENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_paragraphs",
            "description": "Read a range of paragraphs by index. Useful for scanning text between headings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_index": {"type": "integer", "description": "Starting paragraph index (0-based)."},
                    "count": {"type": "integer", "description": "Number of paragraphs to read (default 10)."}
                },
                "required": ["start_index"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_at_paragraph",
            "description": "Insert text at a specific paragraph index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paragraph_index": {"type": "integer", "description": "0-based paragraph index."},
                    "text": {"type": "string", "description": "Text to insert."},
                    "position": {
                        "type": "string",
                        "enum": ["before", "after", "replace"],
                        "description": "Position relative to the target paragraph (default: 'before')."
                    }
                },
                "required": ["paragraph_index", "text"],
                "additionalProperties": False,
            },
        },
    },
]
