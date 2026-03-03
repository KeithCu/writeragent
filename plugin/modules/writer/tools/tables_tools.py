import json
import inspect
from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.tables import tool_list_tables, tool_read_table, tool_write_table_cell

class ListTablesTool(ToolBase):
    name = 'list_tables'
    description = 'List all text tables in the document with their names and dimensions (rows x cols).'
    parameters = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_list_tables
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

class ReadTableTool(ToolBase):
    name = 'read_table'
    description = 'Read all cell contents from a named Writer table as a 2D array.'
    parameters = {'type': 'object', 'properties': {'table_name': {'type': 'string', 'description': 'The table name from list_tables.'}}, 'required': ['table_name'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = False

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_read_table
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

class WriteTableCellTool(ToolBase):
    name = 'write_table_cell'
    description = "Write a value to a specific cell in a named Writer table. Use Excel-style cell references (e.g. 'A1', 'B2'). Numeric strings are stored as numbers automatically."
    parameters = {'type': 'object', 'properties': {'table_name': {'type': 'string', 'description': 'The table name from list_tables.'}, 'cell': {'type': 'string', 'description': "Cell reference, e.g. 'A1', 'B3'."}, 'value': {'type': 'string', 'description': 'The value to write.'}}, 'required': ['table_name', 'cell', 'value'], 'additionalProperties': False}
    doc_types = ['writer']
    tier = 'core'
    is_mutation = True

    def execute(self, ctx, **kwargs):
        dispatch_func = tool_write_table_cell
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

