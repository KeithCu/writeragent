import json
import inspect
from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.comments import tool_list_comments, tool_add_comment, tool_delete_comment

class ListCommentsTool(ToolBase):
    name = 'list_comments'
    description = 'List all comments/annotations in the document, including author, content, date, resolved status, and the text they are anchored to.'
    parameters = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_list_comments
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

class AddCommentTool(ToolBase):
    name = 'add_comment'
    description = 'Add a comment/annotation anchored to the paragraph containing search_text.'
    parameters = {'type': 'object', 'properties': {'content': {'type': 'string', 'description': 'The comment text.'}, 'search_text': {'type': 'string', 'description': 'Anchor the comment to the paragraph containing this text.'}, 'author': {'type': 'string', 'description': 'Author name shown on the comment. Default: AI.'}}, 'required': ['content', 'search_text'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = True

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_add_comment
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

class DeleteCommentTool(ToolBase):
    name = 'delete_comment'
    description = "Delete a comment and all its replies by the comment's name (from list_comments)."
    parameters = {'type': 'object', 'properties': {'comment_name': {'type': 'string', 'description': "The 'name' field of the comment returned by list_comments."}}, 'required': ['comment_name'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = True

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_delete_comment
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

