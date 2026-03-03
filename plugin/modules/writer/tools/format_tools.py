import json
import inspect
from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.format_support import tool_get_document_content, tool_apply_document_content, tool_find_text

class GetDocumentContentTool(ToolBase):
    name = 'get_document_content'
    description = 'Get document (or selection/range) content. Result includes document_length. scope: full, selection, or range (requires start, end).'
    parameters = {'type': 'object', 'properties': {'max_chars': {'type': 'integer', 'description': 'Maximum number of characters to return. Omit for full content.'}, 'scope': {'type': 'string', 'enum': ['full', 'selection', 'range'], 'description': 'Return full document (default), current selection/cursor region, or a character range (requires start and end).'}, 'start': {'type': 'integer', 'description': "Start character offset (0-based). Required when scope is 'range'."}, 'end': {'type': 'integer', 'description': "End character offset (exclusive). Required when scope is 'range'."}}, 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_get_document_content
        status_cb = getattr(ctx, 'status_callback', None)
        thinking_cb = getattr(ctx, 'append_thinking_callback', None)
        sig = inspect.signature(dispatch_func)
        extra = {}
        if 'status_callback' in sig.parameters or 'kwargs' in sig.parameters:
            extra['status_callback'] = status_cb
        if 'append_thinking_callback' in sig.parameters or 'kwargs' in sig.parameters:
            extra['append_thinking_callback'] = thinking_cb
        result_str = dispatch_func(ctx.doc, ctx.ctx, kwargs, **extra)
        try:
            return json.loads(result_str) if isinstance(result_str, str) else result_str
        except (json.JSONDecodeError, TypeError):
            return {'status': 'error', 'message': 'Invalid tool response'}

class ApplyDocumentContentTool(ToolBase):
    name = 'apply_document_content'
    description = "Insert or replace content. Preferred for partial edits: target='search' with search= and content=. For whole doc: target='full'. Use target='range' with start/end (e.g. from find_text or get_document_content document_length)."
    parameters = {'type': 'object', 'properties': {'content': {'type': 'string', 'description': 'The new content (Markdown or HTML based on system prompt). Can be list of strings (joined with newlines).'}, 'target': {'type': 'string', 'enum': ['beginning', 'end', 'selection', 'search', 'full', 'range'], 'description': 'Where to apply: full, range (start+end), search (needs search), beginning, end, selection.'}, 'start': {'type': 'integer', 'description': "Start character offset (0-based). Required when target is 'range'."}, 'end': {'type': 'integer', 'description': "End character offset (exclusive). Required when target is 'range'."}, 'search': {'type': 'string', 'description': "Text to find (LO strips to plain to match). For section replacement send the full section text. Required for target 'search'."}, 'all_matches': {'type': 'boolean', 'description': "When target is 'search', replace all occurrences (true) or just the first (false). Default false."}, 'case_sensitive': {'type': 'boolean', 'description': "When target is 'search', whether the search is case-sensitive. Default true."}}, 'required': ['content', 'target'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = True

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_apply_document_content
        status_cb = getattr(ctx, 'status_callback', None)
        thinking_cb = getattr(ctx, 'append_thinking_callback', None)
        sig = inspect.signature(dispatch_func)
        extra = {}
        if 'status_callback' in sig.parameters or 'kwargs' in sig.parameters:
            extra['status_callback'] = status_cb
        if 'append_thinking_callback' in sig.parameters or 'kwargs' in sig.parameters:
            extra['append_thinking_callback'] = thinking_cb
        result_str = dispatch_func(ctx.doc, ctx.ctx, kwargs, **extra)
        try:
            return json.loads(result_str) if isinstance(result_str, str) else result_str
        except (json.JSONDecodeError, TypeError):
            return {'status': 'error', 'message': 'Invalid tool response'}

class FindTextTool(ToolBase):
    name = 'find_text'
    description = 'Finds text. LO strips search string to plain to match document content. Returns {start, end, text} per match. Use with apply_document_content (search= or target=range).'
    parameters = {'type': 'object', 'properties': {'search': {'type': 'string', 'description': 'Text to search (LO strips to plain to match).'}, 'start': {'type': 'integer', 'description': 'Start offset to search from (default 0).'}, 'limit': {'type': 'integer', 'description': 'Maximum number of matches to return (optional).'}, 'case_sensitive': {'type': 'boolean', 'description': 'Case sensitive search. Default true.'}}, 'required': ['search'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_find_text
        status_cb = getattr(ctx, 'status_callback', None)
        thinking_cb = getattr(ctx, 'append_thinking_callback', None)
        sig = inspect.signature(dispatch_func)
        extra = {}
        if 'status_callback' in sig.parameters or 'kwargs' in sig.parameters:
            extra['status_callback'] = status_cb
        if 'append_thinking_callback' in sig.parameters or 'kwargs' in sig.parameters:
            extra['append_thinking_callback'] = thinking_cb
        result_str = dispatch_func(ctx.doc, ctx.ctx, kwargs, **extra)
        try:
            return json.loads(result_str) if isinstance(result_str, str) else result_str
        except (json.JSONDecodeError, TypeError):
            return {'status': 'error', 'message': 'Invalid tool response'}

