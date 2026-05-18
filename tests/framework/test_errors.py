
import queue
import unittest
import pytest
import sys
import unittest
import json
from unittest.mock import Mock, patch
from plugin.framework.errors import WriterAgentException, format_error_payload, safe_json_loads
from plugin.framework.tool import ToolBase
from plugin.framework.client.errors import format_error_for_display
from plugin.framework.async_stream import StreamQueueKind, run_stream_drain_loop
from unittest.mock import MagicMock, patch
from plugin.framework.errors import WriterError
from plugin.calc.manipulator import CellManipulator
from plugin.calc import CalcError
from plugin.draw.shapes import DrawShapes, DrawError
from plugin.writer.ops import find_paragraph_for_range, get_selection_range, insert_html_at_cursor, get_text_cursor_at_range
from plugin.framework.errors import safe_python_literal_eval

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

class TestWriterModuleErrors():

    def test_find_paragraph_for_range_null_anchor(self):
        with pytest.raises(WriterError) as exc_info:
            find_paragraph_for_range(None, [MagicMock()], MagicMock())
        assert (exc_info.value.code == 'WRITER_ANCHOR_NULL')

    def test_find_paragraph_for_range_empty_ranges(self):
        with pytest.raises(WriterError) as exc_info:
            find_paragraph_for_range(MagicMock(), [], MagicMock())
        assert (exc_info.value.code == 'WRITER_PARA_RANGES_EMPTY')

    def test_find_paragraph_for_range_null_text_obj(self):
        with pytest.raises(WriterError) as exc_info:
            find_paragraph_for_range(MagicMock(), [MagicMock()], None)
        assert (exc_info.value.code == 'WRITER_TEXT_OBJ_NULL')

    def test_get_selection_range_null_model(self):
        with pytest.raises(WriterError) as exc_info:
            get_selection_range(None)
        assert (exc_info.value.code == 'WRITER_MODEL_NULL')

    def test_get_selection_range_null_controller(self):
        mock_model = MagicMock()
        mock_model.getCurrentController.return_value = None
        with pytest.raises(WriterError) as exc_info:
            get_selection_range(mock_model)
        assert (exc_info.value.code == 'WRITER_CONTROLLER_NULL')

    def test_insert_html_at_cursor_null_cursor(self):
        with pytest.raises(WriterError) as exc_info:
            insert_html_at_cursor(None, '<p>Test</p>')
        assert (exc_info.value.code == 'WRITER_CURSOR_NULL')

    def test_insert_html_at_cursor_empty_content(self):
        with pytest.raises(WriterError) as exc_info:
            insert_html_at_cursor(MagicMock(), '')
        assert (exc_info.value.code == 'WRITER_HTML_CONTENT_EMPTY')

    def test_get_text_cursor_at_range_null_model(self):
        with pytest.raises(WriterError) as exc_info:
            get_text_cursor_at_range(None, 0, 10)
        assert (exc_info.value.code == 'WRITER_MODEL_NULL')

    def test_get_text_cursor_at_range_null_offsets(self):
        with pytest.raises(WriterError) as exc_info:
            get_text_cursor_at_range(MagicMock(), None, 10)
        assert (exc_info.value.code == 'WRITER_INVALID_OFFSETS')
        with pytest.raises(WriterError) as exc_info:
            get_text_cursor_at_range(MagicMock(), 0, None)
        assert (exc_info.value.code == 'WRITER_INVALID_OFFSETS')

    @patch('plugin.doc.document_helpers.get_document_length', return_value=100)
    def test_get_text_cursor_at_range_null_text_obj(self, mock_doc_len):
        mock_model = MagicMock()
        mock_model.getText.return_value = None
        with pytest.raises(WriterError) as exc_info:
            get_text_cursor_at_range(mock_model, 0, 10)
        assert (exc_info.value.code == 'WRITER_TEXT_NOT_FOUND')

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

    def test_glm45_deserializer(self):
        from plugin.contrib.tool_call_parsers.glm45_parser import _deserialize_value
        self.assertEqual(_deserialize_value('True'), True)
        self.assertEqual(_deserialize_value('true'), True)
        self.assertEqual(_deserialize_value('123'), 123)
        self.assertEqual(_deserialize_value("'abc'"), 'abc')

    def test_qwen3_coder_deserializer(self):
        from plugin.contrib.tool_call_parsers.qwen3_coder_parser import _try_convert_value
        self.assertEqual(_try_convert_value('True'), True)
        self.assertEqual(_try_convert_value('null'), None)
        self.assertEqual(_try_convert_value('123'), 123)

    def test_smolagents_deserializer(self):
        self.assertEqual(safe_python_literal_eval('{"type": "string"}'), {'type': 'string'})
if (__name__ == '__main__'):
    unittest.main()
