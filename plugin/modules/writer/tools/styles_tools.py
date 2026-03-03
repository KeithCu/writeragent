import json
import inspect
from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.styles import tool_list_styles, tool_get_style_info

class ListStylesTool(ToolBase):
    name = 'list_styles'
    description = 'List available styles in the document. Call this before applying styles with apply_document_content to discover exact style names (they may be localized). family defaults to ParagraphStyles.'
    parameters = {'type': 'object', 'properties': {'family': {'type': 'string', 'description': 'Style family to list.', 'enum': ['ParagraphStyles', 'CharacterStyles', 'PageStyles', 'FrameStyles', 'NumberingStyles']}}, 'required': [], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_list_styles
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

class GetStyleInfoTool(ToolBase):
    name = 'get_style_info'
    description = 'Get detailed properties of a specific style (font, size, margins, etc.).'
    parameters = {'type': 'object', 'properties': {'style_name': {'type': 'string', 'description': 'Name of the style to inspect.'}, 'family': {'type': 'string', 'description': 'Style family. Default: ParagraphStyles.', 'enum': ['ParagraphStyles', 'CharacterStyles', 'PageStyles', 'FrameStyles', 'NumberingStyles']}}, 'required': ['style_name'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_get_style_info
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

