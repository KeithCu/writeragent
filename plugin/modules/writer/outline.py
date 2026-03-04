import json
from plugin.modules.core.services.document import build_heading_tree, resolve_locator, get_paragraph_ranges

def _err(message):
    return json.dumps({"status": "error", "message": message})

def tool_get_document_outline(model, ctx, args):
    """Return a hierarchical heading tree (outline) of the document."""
    try:
        tree = build_heading_tree(model)
        # Root node children are the top-level headings
        return json.dumps({"status": "ok", "outline": tree["children"]})
    except Exception as e:
        return _err(str(e))

def tool_get_heading_content(model, ctx, args):
    """Return text and sub-headings for a specific heading locator."""
    locator = args.get("locator", "")
    if not locator:
        return _err("locator is required (e.g. heading:1.2)")
    try:
        res = resolve_locator(model, locator)
        para_idx = res.get("para_index", 0)
        
        # Build tree to find the node and its children
        tree = build_heading_tree(model)
        def find_node(node, p_idx):
            if node.get("para_index") == p_idx:
                return node
            for child in node.get("children", []):
                found = find_node(child, p_idx)
                if found:
                    return found
            return None
        
        node = find_node(tree, para_idx)
        if not node:
            return _err(f"Heading at {locator} not found")
            
        # Get body text between this heading and the next heading
        text_parts = []
        ranges = get_paragraph_ranges(model)
        for i in range(para_idx + 1, len(ranges)):
            p = ranges[i]
            if p.supportsService("com.sun.star.text.Paragraph"):
                if p.getPropertyValue("OutlineLevel") > 0:
                    break
                text_parts.append(p.getString())
            elif p.supportsService("com.sun.star.text.TextTable"):
                break # Stop at tables for now like the extension does in some paths
                
        return json.dumps({
            "status": "ok",
            "locator": locator,
            "text": "\n".join(text_parts),
            "sub_headings": node.get("children", [])
        })
    except Exception as e:
        return _err(str(e))

OUTLINE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_document_outline",
            "description": "Get a hierarchical heading tree (outline) of the document.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_heading_content",
            "description": "Read text and sub-headings for a specific heading locator (e.g. 'heading:1.2').",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": {"type": "string", "description": "Heading locator, e.g. 'heading:1' or 'heading:2.1'."}
                },
                "required": ["locator"],
                "additionalProperties": False,
            },
        },
    },
]
