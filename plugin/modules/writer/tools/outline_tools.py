import json
import inspect
from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.outline import tool_get_document_outline, tool_get_heading_content
from plugin.modules.writer.content import tool_read_paragraphs, tool_insert_at_paragraph

class GetDocumentOutlineTool(ToolBase):
    name = 'get_document_outline'
    description = 'Get a hierarchical heading tree (outline) of the document.'
    parameters = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_get_document_outline
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

class GetHeadingContentTool(ToolBase):
    name = 'get_heading_content'
    description = "Read text and sub-headings for a specific heading locator (e.g. 'heading:1.2')."
    parameters = {'type': 'object', 'properties': {'locator': {'type': 'string', 'description': "Heading locator, e.g. 'heading:1' or 'heading:2.1'."}}, 'required': ['locator'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_get_heading_content
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

class ReadParagraphsTool(ToolBase):
    name = 'read_paragraphs'
    description = 'Read a range of paragraphs by index. Useful for scanning text between headings.'
    parameters = {'type': 'object', 'properties': {'start_index': {'type': 'integer', 'description': 'Starting paragraph index (0-based).'}, 'count': {'type': 'integer', 'description': 'Number of paragraphs to read (default 10).'}}, 'required': ['start_index'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_read_paragraphs
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

class InsertAtParagraphTool(ToolBase):
    name = 'insert_at_paragraph'
    description = 'Insert text at a specific paragraph index.'
    parameters = {'type': 'object', 'properties': {'paragraph_index': {'type': 'integer', 'description': '0-based paragraph index.'}, 'text': {'type': 'string', 'description': 'Text to insert.'}, 'position': {'type': 'string', 'enum': ['before', 'after', 'replace'], 'description': "Position relative to the target paragraph (default: 'before')."}}, 'required': ['paragraph_index', 'text'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = True

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_insert_at_paragraph
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

