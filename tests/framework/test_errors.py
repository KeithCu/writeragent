
import queue
import unittest
import pytest
import sys
from plugin.framework.errors import WriterAgentException, format_error_payload, safe_json_loads
from plugin.framework.tool import ToolBase
from plugin.framework.client.errors import format_error_for_display
from plugin.framework.async_stream import StreamQueueKind, run_stream_drain_loop
from unittest.mock import MagicMock
from plugin.framework.errors import WriterError
from plugin.calc.manipulator import CellManipulator
from plugin.calc import CalcError
from plugin.draw.shapes import DrawShapes, DrawError
from plugin.framework.errors import safe_python_literal_eval
import inspect


def _suppress_disposed_debug_logs_present() -> bool:
    """False in stripped release trees where log.debug/info call sites are removed."""
    from plugin.framework.errors import suppress_disposed

    try:
        return "log_obj.debug" in inspect.getsource(suppress_disposed)
    except OSError:
        return False

class DummyTool(ToolBase):
    name = 'dummy_tool'
    description = 'Dummy Tool'

    def execute(self, **kwargs):
        pass

class TestErrorHandling(unittest.TestCase):

    def test_format_error_payload_writer_agent_exception(self):
        exc = WriterAgentException('Test message', code='TEST_CODE', details={'key': 'value'})
        payload = format_error_payload(exc)
        self.assertEqual(payload['status'], 'error')
        self.assertEqual(payload['code'], 'TEST_CODE')
        self.assertEqual(payload['message'], 'Test message')
        self.assertEqual(payload['details'], {'key': 'value'})

    def test_format_error_payload_generic_exception(self):
        exc = ValueError('Test message')
        payload = format_error_payload(exc)
        self.assertEqual(payload['status'], 'error')
        self.assertEqual(payload['code'], 'INTERNAL_ERROR')
        self.assertEqual(payload['message'], 'Test message')
        self.assertEqual(payload['details'], {'type': 'ValueError'})

    def test_tool_base_error_formatting(self):
        tool = DummyTool()
        result = tool._tool_error('Tool failed', code='CUSTOM_CODE', key='val')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['code'], 'CUSTOM_CODE')
        self.assertEqual(result['message'], 'Tool failed')
        self.assertEqual(result['details'], {'key': 'val'})

    def test_format_error_for_display(self):
        exc = WriterAgentException('User error', code='ERR')
        display_str = format_error_for_display(exc)
        self.assertEqual(display_str, 'Error: User error')
        exc_generic = ValueError('System error')
        display_str_generic = format_error_for_display(exc_generic)
        self.assertEqual(display_str_generic, 'Error: System error')

    def test_format_error_for_display_payload_dict(self):
        display_str = format_error_for_display(
            {"status": "error", "code": "HTTP_ERROR", "message": "HTTP Error 500 from AI Provider"}
        )
        self.assertEqual(display_str, "Error: HTTP Error 500 from AI Provider")

    def test_format_error_for_display_payload_dict_missing_message(self):
        display_str = format_error_for_display({"status": "error", "code": "HTTP_ERROR"})
        self.assertEqual(display_str, "Error: HTTP_ERROR")
        display_empty = format_error_for_display({})
        self.assertEqual(display_empty, "Error: {}")

    def test_librepy_exceptions_hierarchy_and_codes(self):
        from plugin.framework.errors import (
            CalcError,
            DataShapeError,
            ExcelConversionError,
            FormulaError,
            FormulaSyntaxError,
            PayloadCodecError,
            SandboxSecurityError,
            ScriptingError,
            SpillCollisionError,
            VenvError,
            VenvExecutionError,
            VenvNotFoundError,
            VenvTimeoutError,
            WorkerIPCError,
        )

        # Test base hierarchy and default error codes
        cases = [
            (ScriptingError("script fail"), "SCRIPTING_ERROR", ScriptingError),
            (VenvError("venv fail"), "VENV_ERROR", ScriptingError),
            (VenvNotFoundError("venv missing"), "VENV_NOT_FOUND", VenvError),
            (VenvTimeoutError("timed out"), "VENV_TIMEOUT", ScriptingError),
            (VenvExecutionError("crash"), "VENV_EXEC_ERROR", ScriptingError),
            (WorkerIPCError("pipe broken"), "WORKER_IPC_ERROR", ScriptingError),
            (CalcError("calc error"), "CALC_ERROR", WriterAgentException),
            (FormulaError("bad formula"), "FORMULA_ERROR", CalcError),
            (FormulaSyntaxError("syntax"), "FORMULA_SYNTAX_ERROR", FormulaError),
            (SpillCollisionError("blocked"), "SPILL_COLLISION", FormulaError),
            (ExcelConversionError("conversion fail"), "EXCEL_CONVERSION_ERROR", CalcError),
            (SandboxSecurityError("blocked import"), "SANDBOX_SECURITY_ERROR", ScriptingError),
            (PayloadCodecError("codec fail"), "PAYLOAD_CODEC_ERROR", ScriptingError),
            (DataShapeError("too large"), "DATA_SHAPE_ERROR", PayloadCodecError),
        ]

        for exc, expected_code, parent_cls in cases:
            self.assertIsInstance(exc, WriterAgentException)
            self.assertIsInstance(exc, parent_cls)
            self.assertEqual(exc.code, expected_code)
            payload = format_error_payload(exc)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["code"], expected_code)

    def test_librepy_format_error_message_advice(self):
        from plugin.framework.errors import (
            SandboxSecurityError,
            SpillCollisionError,
            VenvNotFoundError,
            VenvTimeoutError,
            format_error_message,
        )

        msg_venv = format_error_message(VenvNotFoundError("No python executable found"))
        self.assertIn("Python venv not found", msg_venv)

        msg_timeout = format_error_message(VenvTimeoutError("Python timed out after 30 seconds"))
        self.assertIn("Python execution timed out", msg_timeout)

        msg_spill = format_error_message(SpillCollisionError("#SPILL!"))
        self.assertIn("Formula spill collision", msg_spill)

        msg_sandbox = format_error_message(SandboxSecurityError("Import os forbidden"))
        self.assertIn("Script execution blocked by sandbox policy", msg_sandbox)

        msg_generic_venv = format_error_message(RuntimeError("venv not found at /tmp/x"))
        self.assertIn("Python venv not found", msg_generic_venv)

    def test_format_error_message_http_exception(self):
        import http.client
        from plugin.framework.errors import format_error_message

        err = http.client.RemoteDisconnected("Remote end closed connection")
        msg = format_error_message(err)
        self.assertTrue("HTTP Error" in msg or "Remote" in msg)
        self.assertNotIn("Connection Error", msg)

    def test_writeragent_exception_subclasses_and_details_unification(self):
        from plugin.framework.errors import (
            AgentParsingError,
            ConfigError,
            ConfigValidationError,
            DocumentDisposedError,
            NetworkError,
            ResourceNotFoundError,
            ToolContextError,
            ToolExecutionError,
            ToolPermissionError,
            UnoObjectError,
            WorkerPoolError,
        )
        from plugin.draw.shapes import DrawError
        from plugin.mcp.mcp_protocol import BusyError

        subclasses = [
            (ConfigError("cfg err"), "CONFIG_ERROR"),
            (ConfigValidationError("val err"), "CONFIG_VALIDATION_ERROR"),
            (NetworkError("net err"), "NETWORK_ERROR"),
            (UnoObjectError("uno err"), "UNO_OBJECT_ERROR"),
            (WorkerPoolError("worker err"), "WORKER_ERROR"),
            (ToolExecutionError("tool err"), "TOOL_EXECUTION_ERROR"),
            (ToolPermissionError("perm err"), "PERMISSION_DENIED"),
            (ToolContextError("ctx err"), "CONTEXT_ERROR"),
            (WriterError("writer err"), "WRITER_ERROR"),
            (AgentParsingError("parse err"), "PARSE_ERROR"),
            (DrawError("draw err"), "DRAW_ERROR"),
            (BusyError("busy err"), "SERVER_BUSY"),
        ]

        for exc, expected_code in subclasses:
            self.assertEqual(exc.code, expected_code)
            self.assertEqual(exc.details, {})
            self.assertEqual(exc.context, {})
            payload = format_error_payload(exc)
            self.assertEqual(payload["code"], expected_code)
            self.assertNotIn("details", payload)

        # Test overriding code and passing details
        custom_exc = ConfigError("bad config", code="CUSTOM_CFG", details={"key": "api_key"})
        self.assertEqual(custom_exc.code, "CUSTOM_CFG")
        self.assertEqual(custom_exc.details, {"key": "api_key"})
        self.assertEqual(custom_exc.context, {"key": "api_key"})
        payload = format_error_payload(custom_exc)
        self.assertEqual(payload["code"], "CUSTOM_CFG")
        self.assertEqual(payload["details"], {"key": "api_key"})

        # Test legacy context= parameter backward compatibility
        legacy_exc = NetworkError("timeout", context={"url": "http://localhost"})
        self.assertEqual(legacy_exc.code, "NETWORK_ERROR")
        self.assertEqual(legacy_exc.details, {"url": "http://localhost"})
        self.assertEqual(legacy_exc.context, {"url": "http://localhost"})
        payload = format_error_payload(legacy_exc)
        self.assertEqual(payload["details"], {"url": "http://localhost"})

        # Test DocumentDisposedError with custom field object_type
        disp_exc = DocumentDisposedError("Object disposed", object_type="TextRange", details={"line": 42})
        self.assertEqual(disp_exc.code, "DISPOSED_OBJECT")
        self.assertEqual(disp_exc.object_type, "TextRange")
        self.assertEqual(disp_exc.details, {"line": 42})

        # Test ResourceNotFoundError with custom fields resource_type and identifier
        res_exc = ResourceNotFoundError("Template", "default.ott", details={"path": "/tmp"})
        self.assertEqual(res_exc.code, "RESOURCE_NOT_FOUND")
        self.assertEqual(res_exc.resource_type, "Template")
        self.assertEqual(res_exc.identifier, "default.ott")
        self.assertEqual(res_exc.details, {"path": "/tmp"})
        self.assertIn("Template not found: default.ott", str(res_exc))


class TestSafeJsonLoads(unittest.TestCase):

    def test_safe_json_loads_valid(self):
        self.assertEqual(safe_json_loads('{"key": "value"}'), {'key': 'value'})
        self.assertEqual(safe_json_loads('[1, 2, 3]'), [1, 2, 3])
        self.assertEqual(safe_json_loads('"string"'), 'string')
        self.assertEqual(safe_json_loads('123'), 123)

    def test_safe_json_loads_repair_truncated(self):
        self.assertEqual(safe_json_loads('{"key": "value"'), {'key': 'value'})
        self.assertEqual(safe_json_loads('[1, 2'), [1, 2])
        self.assertEqual(safe_json_loads('{"a": {"b": 1'), {'a': {'b': 1}})

    def test_safe_json_loads_repair_trailing_comma(self):
        self.assertEqual(safe_json_loads('{"key": "value",}'), {'key': 'value'})
        self.assertEqual(safe_json_loads('[1, 2, ]'), [1, 2])

    def test_safe_json_loads_literal_eval(self):
        self.assertEqual(safe_json_loads("{'key': 'value'}"), {'key': 'value'})
        self.assertEqual(safe_json_loads('[True, False, None]'), [True, False, None])

    def test_safe_json_loads_invalid(self):
        self.assertIsNone(safe_json_loads('not json at all'))
        self.assertIsNone(safe_json_loads('<<< completely broken garbage >>>'))

    def test_safe_json_loads_wrong_type(self):
        self.assertIsNone(safe_json_loads(None))
        self.assertIsNone(safe_json_loads(123))
        self.assertIsNone(safe_json_loads({'not': 'a string'}))

    def test_safe_json_loads_null_eval(self):
        self.assertIsNone(safe_json_loads('null'))
        self.assertEqual(safe_json_loads('null', default={}), {})

    def test_safe_json_loads_custom_default(self):
        self.assertEqual(safe_json_loads('invalid', default={'error': True}), {'error': True})
        self.assertEqual(safe_json_loads(None, default='default'), 'default')

    def test_safe_json_loads_silent_latex_corruption(self):
        corrupted_json = '{"content": "\nabla \times \x0crac{1}{c}"}'
        repaired = safe_json_loads(corrupted_json)
        self.assertEqual(repaired, {'content': '\\nabla \\times \\frac{1}{c}'})

class TestAsyncStreamErrorHandling(unittest.TestCase):

    def test_run_stream_drain_loop_error_handler(self):
        q = queue.Queue()
        job_done = [False]
        test_error = ValueError('Simulation error')
        formatted_error = format_error_payload(test_error)
        q.put((StreamQueueKind.ERROR, formatted_error))
        error_received = []

        def on_error(e):
            error_received.append(e)

        class DummyToolkit():

            def processEventsToIdle(self):
                pass
        run_stream_drain_loop(q, DummyToolkit(), job_done, (lambda c, t: None), on_error=on_error, on_stream_done=(lambda x: True), on_stopped=(lambda : None))
        self.assertTrue(job_done[0])
        self.assertEqual(len(error_received), 1)
        self.assertEqual(error_received[0]['status'], 'error')
        self.assertEqual(error_received[0]['message'], 'Simulation error')
sys.modules['uno'] = MagicMock()
sys.modules['unohelper'] = MagicMock()
sys.modules['com.sun.star.beans'] = MagicMock()
sys.modules['com.sun.star.table'] = MagicMock()

class MockBase():
    pass
sys.modules['unohelper'].Base = MockBase

@pytest.fixture
def mock_bridge():
    return MagicMock()

@pytest.fixture
def manipulator(mock_bridge):
    return CellManipulator(mock_bridge)

def test_safe_get_cell_value_sheet_none(manipulator):
    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(None, 'A1')
    assert (exc_info.value.code == 'CALC_SHEET_NULL')
    assert ('Sheet is None' in exc_info.value.message)

def test_safe_get_cell_value_invalid_address(manipulator):
    sheet = MagicMock()
    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, '1A')
    assert (exc_info.value.code == 'CALC_INVALID_ADDRESS')
    assert ('Invalid cell address' in exc_info.value.message)

def test_safe_get_cell_value_cell_not_found(manipulator):
    sheet = MagicMock()
    sheet.getCellRangeByName.side_effect = Exception('Not found')
    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, 'A1')
    assert (exc_info.value.code == 'CALC_CELL_NOT_FOUND')

def test_safe_get_cell_value_empty(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.EMPTY
    sheet.getCellRangeByName.return_value = cell
    assert (manipulator.safe_get_cell_value(sheet, 'A1') is None)

def test_safe_get_cell_value_value(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.VALUE
    cell.getValue.return_value = 42.0
    sheet.getCellRangeByName.return_value = cell
    assert (manipulator.safe_get_cell_value(sheet, 'A1') == 42.0)

def test_safe_get_cell_value_text(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.TEXT
    cell.getString.return_value = 'Hello'
    sheet.getCellRangeByName.return_value = cell
    assert (manipulator.safe_get_cell_value(sheet, 'A1') == 'Hello')

def test_safe_get_cell_value_formula_success(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.FORMULA
    cell.getError.return_value = 0
    cell.getValue.return_value = 100.0
    sheet.getCellRangeByName.return_value = cell
    assert (manipulator.safe_get_cell_value(sheet, 'A1') == 100.0)

def test_safe_get_cell_value_formula_error(manipulator):
    from com.sun.star.table import CellContentType as CCT
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = CCT.FORMULA
    cell.getError.return_value = 503
    sheet.getCellRangeByName.return_value = cell
    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, 'A1')
    assert (exc_info.value.code == 'CALC_FORMULA_ERROR')
    assert ('Formula error in A1: #NUM!' in exc_info.value.message)
    assert (exc_info.value.details['error_code'] == 503)
    assert (exc_info.value.details['error_name'] == '#NUM!')

def test_safe_get_cell_value_unknown_type(manipulator):
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = 999
    sheet.getCellRangeByName.return_value = cell
    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, 'A1')
    assert (exc_info.value.code == 'CALC_UNKNOWN_CELL_TYPE')

def test_safe_get_cell_value_unexpected_error(manipulator):
    sheet = MagicMock()
    cell = MagicMock()
    cell.getType.side_effect = RuntimeError('Something bad happened')
    sheet.getCellRangeByName.return_value = cell
    with pytest.raises(CalcError) as exc_info:
        manipulator.safe_get_cell_value(sheet, 'A1')
    assert (exc_info.value.code == 'CALC_CELL_VALUE_ERROR')
    assert ('Failed to get cell value' in exc_info.value.message)

def test_draw_shapes_safe_create_shape_valid():
    'Test safe_create_shape with valid inputs creates and adds the shape.'
    draw_shapes = DrawShapes()
    doc = MagicMock()
    page = MagicMock()
    shape = MagicMock()
    doc.createInstance.return_value = shape
    position = MagicMock()
    position.X = 100
    position.Y = 200
    size = MagicMock()
    size.Width = 300
    size.Height = 400
    shape_type = 'RectangleShape'
    (result, geom_ok, geom_err) = draw_shapes.safe_create_shape(doc, page, shape_type, position, size)
    doc.createInstance.assert_called_once_with('com.sun.star.drawing.RectangleShape')
    shape.setPosition.assert_called_once_with(position)
    shape.setSize.assert_called_once_with(size)
    page.add.assert_called_once_with(shape)
    assert (result == shape)
    assert ((geom_ok is None) and (geom_err is None))

def test_draw_shapes_safe_create_shape_invalid_page():
    'Test safe_create_shape raises DrawError when page is None.'
    draw_shapes = DrawShapes()
    doc = MagicMock()
    position = MagicMock()
    position.X = 100
    position.Y = 200
    size = MagicMock()
    size.Width = 300
    size.Height = 400
    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(doc, None, 'RectangleShape', position, size)
    assert (exc_info.value.code == 'DRAW_PAGE_NULL')

def test_draw_shapes_safe_create_shape_invalid_position():
    'Test safe_create_shape raises DrawError when position is invalid.'
    draw_shapes = DrawShapes()
    doc = MagicMock()
    page = MagicMock()
    position = MagicMock()
    del position.X
    size = MagicMock()
    size.Width = 300
    size.Height = 400
    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(doc, page, 'RectangleShape', position, size)
    assert (exc_info.value.code == 'DRAW_INVALID_POSITION')

def test_draw_shapes_safe_create_shape_invalid_size():
    'Test safe_create_shape raises DrawError when size is invalid.'
    draw_shapes = DrawShapes()
    doc = MagicMock()
    page = MagicMock()
    position = MagicMock()
    position.X = 100
    position.Y = 200
    size = MagicMock()
    size.Width = 0
    size.Height = 400
    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(doc, page, 'RectangleShape', position, size)
    assert (exc_info.value.code == 'DRAW_INVALID_SIZE')

def test_draw_shapes_safe_create_shape_creation_failed():
    'Test safe_create_shape raises DrawError when shape creation fails.'
    draw_shapes = DrawShapes()
    doc = MagicMock()
    doc.createInstance.return_value = None
    page = MagicMock()
    position = MagicMock()
    position.X = 100
    position.Y = 200
    size = MagicMock()
    size.Width = 300
    size.Height = 400
    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(doc, page, 'UnknownShape', position, size)
    assert (exc_info.value.code == 'DRAW_SHAPE_CREATION_FAILED')

def test_draw_shapes_safe_create_shape_exception_handling():
    'Test safe_create_shape wraps generic exceptions in DrawError.'
    draw_shapes = DrawShapes()
    doc = MagicMock()
    doc.createInstance.side_effect = Exception('Some UNO error')
    page = MagicMock()
    position = MagicMock()
    position.X = 100
    position.Y = 200
    size = MagicMock()
    size.Width = 300
    size.Height = 400
    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(doc, page, 'RectangleShape', position, size)
    assert (exc_info.value.code == 'DRAW_SHAPE_CREATION_ERROR')
    assert ('Some UNO error' in exc_info.value.details['original_error'])


class TestSecurityFix(unittest.TestCase):

    def test_nested_structures_no_crash(self):
        depth = 5000
        nested_list_str = (('[' * depth) + (']' * depth))
        try:
            result = safe_python_literal_eval(nested_list_str, default='fallback')
            self.assertTrue((isinstance(result, list) or (result == 'fallback')))
        except Exception as e:
            self.fail(f'safe_python_literal_eval crashed with {type(e).__name__}: {e}')

    def test_large_input_no_crash(self):
        large_input = (('[' + ('1,' * 1000000)) + '1]')
        try:
            result = safe_python_literal_eval(large_input, default='fallback')
            self.assertTrue((isinstance(result, list) or (result == 'fallback')))
        except Exception as e:
            self.fail(f'safe_python_literal_eval crashed with {type(e).__name__}: {e}')

    def test_common_literals(self):
        self.assertEqual(safe_python_literal_eval('True'), True)
        self.assertEqual(safe_python_literal_eval('true'), True)
        self.assertEqual(safe_python_literal_eval('False'), False)
        self.assertEqual(safe_python_literal_eval('false'), False)
        self.assertEqual(safe_python_literal_eval('None'), None)
        self.assertEqual(safe_python_literal_eval('none'), None)
        self.assertEqual(safe_python_literal_eval('null'), None)
        self.assertEqual(safe_python_literal_eval('NULL'), None)
        self.assertEqual(safe_python_literal_eval('123'), 123)
        self.assertEqual(safe_python_literal_eval('"hello"'), 'hello')
        self.assertEqual(safe_python_literal_eval("'hello'"), 'hello')

    def test_json_structures(self):
        self.assertEqual(safe_python_literal_eval('[1, 2, 3]'), [1, 2, 3])
        self.assertEqual(safe_python_literal_eval('{"a": 1}'), {'a': 1})

    def test_single_quoted_strings_restricted(self):
        self.assertEqual(safe_python_literal_eval("'safe'"), 'safe')
        self.assertEqual(safe_python_literal_eval("'it\\'s unsafe'", default='fallback'), 'fallback')

    def test_non_json_python_literals_fallback(self):
        self.assertEqual(safe_python_literal_eval('(1, 2)', default='(1, 2)'), '(1, 2)')
        self.assertEqual(safe_python_literal_eval("{'a': 1}", default='fallback'), 'fallback')

    def test_smolagents_deserializer(self):
        self.assertEqual(safe_python_literal_eval('{"type": "string"}'), {'type': 'string'})


class TestSuppressDisposed(unittest.TestCase):

    def test_check_disposed_none_raises_dummy_ok(self):
        from plugin.framework.errors import UnoObjectError, check_disposed

        with self.assertRaises(UnoObjectError):
            check_disposed(None, "Document Model")
        check_disposed(object(), "Document Model")

    def test_is_disposed_exception(self):
        from plugin.framework.errors import (
            DocumentDisposedError,
            is_disposed_exception,
        )

        class CustomDisposedException(Exception):
            pass

        class CustomRuntimeException(Exception):
            pass

        class UnrelatedError(Exception):
            pass

        self.assertTrue(is_disposed_exception(DocumentDisposedError("Object disposed")))
        self.assertTrue(is_disposed_exception(CustomDisposedException("Disposed")))
        self.assertTrue(is_disposed_exception(CustomRuntimeException("Runtime UNO error")))
        self.assertFalse(is_disposed_exception(UnrelatedError("Regular failure")))
        self.assertFalse(is_disposed_exception(ValueError("Bad value")))

    def test_is_tool_document_disposed_live_doc_bare_runtime(self):
        from plugin.framework.errors import (
            DocumentDisposedError,
            is_tool_document_disposed,
        )

        class CustomDisposedException(Exception):
            pass

        class CustomRuntimeException(Exception):
            pass

        class LiveDoc:
            def getImplementationName(self):
                return "SwXTextDocument"

        class DeadDoc:
            def getImplementationName(self):
                raise CustomDisposedException("gone")

        live = LiveDoc()
        # Bare RuntimeException from a live doc is a real UNO error, not dispose.
        self.assertFalse(is_tool_document_disposed(CustomRuntimeException(""), live))
        self.assertTrue(is_tool_document_disposed(CustomDisposedException("Disposed"), live))
        self.assertTrue(is_tool_document_disposed(DocumentDisposedError("gone"), live))
        # No live probe: keep the lifecycle heuristic.
        self.assertTrue(is_tool_document_disposed(CustomRuntimeException(""), None))
        self.assertTrue(is_tool_document_disposed(CustomRuntimeException(""), DeadDoc()))
        self.assertFalse(is_tool_document_disposed(ValueError("Bad value"), live))

    def test_suppress_disposed_with_disposed_error(self):
        from plugin.framework.errors import (
            DocumentDisposedError,
            suppress_disposed,
        )

        mock_logger = MagicMock()
        executed = False
        with suppress_disposed("test_disposed_action", logger=mock_logger):
            executed = True
            raise DocumentDisposedError("Model was disposed")

        self.assertTrue(executed)
        if _suppress_disposed_debug_logs_present():
            mock_logger.debug.assert_called_once()
            self.assertEqual(mock_logger.debug.call_args[0][1], "test_disposed_action")
        else:
            mock_logger.debug.assert_not_called()
        mock_logger.exception.assert_not_called()

    def test_suppress_disposed_unexpected_error_suppressed(self):
        from plugin.framework.errors import suppress_disposed

        mock_logger = MagicMock()
        executed = False
        with suppress_disposed("test_unexpected_action", logger=mock_logger, suppress_all=True):
            executed = True
            raise ValueError("Something unexpected")

        self.assertTrue(executed)
        mock_logger.exception.assert_called_once()
        self.assertEqual(mock_logger.exception.call_args[0][1], "test_unexpected_action")
        mock_logger.debug.assert_not_called()

    def test_suppress_disposed_unexpected_error_raised(self):
        from plugin.framework.errors import suppress_disposed

        mock_logger = MagicMock()
        with self.assertRaises(ValueError):
            with suppress_disposed("test_raise_action", logger=mock_logger, suppress_all=False):
                raise ValueError("Must be raised")

        mock_logger.exception.assert_called_once()
        self.assertEqual(mock_logger.exception.call_args[0][1], "test_raise_action")

    def test_suppress_disposed_as_decorator(self):
        from plugin.framework.errors import (
            DocumentDisposedError,
            ignore_disposed,
            suppress_disposed,
        )

        mock_logger = MagicMock()

        @suppress_disposed("decorated_function", logger=mock_logger)
        def faulty_fn():
            raise DocumentDisposedError("Peer disposed")

        result = faulty_fn()
        self.assertIsNone(result)
        if _suppress_disposed_debug_logs_present():
            mock_logger.debug.assert_called_once()
        else:
            mock_logger.debug.assert_not_called()
        self.assertIs(ignore_disposed, suppress_disposed)

    def test_safe_uno_call_returns_default_on_runtime_error(self):
        from plugin.framework.errors import safe_uno_call

        @safe_uno_call(default="default_value")
        def _failing_fn():
            raise RuntimeError("bridge error")

        self.assertEqual(_failing_fn(), "default_value")

    def test_safe_uno_call_re_raises_disposed_exception(self):
        from plugin.framework.errors import DocumentDisposedError, safe_uno_call

        @safe_uno_call(default="default_value")
        def _disposed_fn():
            raise DocumentDisposedError("Object was disposed")

        with self.assertRaises(DocumentDisposedError):
            _disposed_fn()


if (__name__ == '__main__'):
    unittest.main()
