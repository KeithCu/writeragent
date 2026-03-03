import json
import inspect
from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.tracking import tool_set_track_changes, tool_get_tracked_changes, tool_accept_all_changes, tool_reject_all_changes

class SetTrackChangesTool(ToolBase):
    name = 'set_track_changes'
    description = 'Enable or disable track changes (change recording) in the document.'
    parameters = {'type': 'object', 'properties': {'enabled': {'type': 'boolean', 'description': 'True to enable track changes, False to disable.'}}, 'required': ['enabled'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = True

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_set_track_changes
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

class GetTrackedChangesTool(ToolBase):
    name = 'get_tracked_changes'
    description = 'List all tracked changes (redlines) in the document, including type, author, date, and comment.'
    parameters = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_get_tracked_changes
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

class AcceptAllChangesTool(ToolBase):
    name = 'accept_all_changes'
    description = 'Accept all tracked changes in the document.'
    parameters = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = True

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_accept_all_changes
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

class RejectAllChangesTool(ToolBase):
    name = 'reject_all_changes'
    description = 'Reject all tracked changes in the document.'
    parameters = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = True

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_reject_all_changes
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

