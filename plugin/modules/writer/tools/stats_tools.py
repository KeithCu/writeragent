import json
import inspect
from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.stats import tool_get_document_stats

class GetDocumentStatsTool(ToolBase):
    name = 'get_document_stats'
    description = 'Get document statistics: character count, paragraph count, and total pages.'
    parameters = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_get_document_stats
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

