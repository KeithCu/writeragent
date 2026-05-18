
import unittest
import json
from plugin.framework.logging import SafeLogger, safe_log_exception
from unittest.mock import MagicMock
from plugin.framework.logging import LOG_REDACT_AUDIO_PLACEHOLDER, LOG_REDACT_IMAGE_PLACEHOLDER, redact_sensitive_payload_for_log
from plugin.framework.logging import format_tool_call_for_display, format_tool_result_for_display, update_activity_state, _activity_state

class TestLogRedaction(unittest.TestCase):

    def test_redact_chat_multimodal(self) -> None:
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}, {'type': 'input_audio', 'input_audio': {'data': 'AAAABBBB', 'format': 'wav'}}, {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,ZZZZ'}}]}]
        out = redact_sensitive_payload_for_log(messages)
        self.assertIsNot(out, messages)
        parts = out[0]['content']
        self.assertEqual(parts[0], {'type': 'text', 'text': 'hi'})
        self.assertEqual(parts[1]['input_audio']['data'], (LOG_REDACT_AUDIO_PLACEHOLDER % 8))
        self.assertEqual(parts[2]['image_url']['url'], (LOG_REDACT_IMAGE_PLACEHOLDER % len('data:image/jpeg;base64,ZZZZ')))
        self.assertEqual(messages[0]['content'][1]['input_audio']['data'], 'AAAABBBB')

    def test_redact_image_generations_request_body(self) -> None:
        u = ('data:image/png;base64,' + ('x' * 100))
        data = {'prompt': 'p', 'image_url': u}
        r = redact_sensitive_payload_for_log(data)
        self.assertEqual(r['prompt'], 'p')
        self.assertEqual(r['image_url'], (LOG_REDACT_IMAGE_PLACEHOLDER % len(u)))

    def test_redact_image_api_response_nested(self) -> None:
        raw = {'data': [{'b64_json': 'Ym9n', 'url': 'http://ok'}, {'url': 'data:image/png;base64,QQ=='}]}
        r = redact_sensitive_payload_for_log(raw)
        self.assertEqual(r['data'][0]['b64_json'], (LOG_REDACT_IMAGE_PLACEHOLDER % 4))
        self.assertEqual(r['data'][0]['url'], 'http://ok')
        self.assertEqual(r['data'][1]['url'], (LOG_REDACT_IMAGE_PLACEHOLDER % len('data:image/png;base64,QQ==')))

def test_format_tool_call_for_display():
    assert (format_tool_call_for_display('my_tool', {'arg': 'val'}) == "my_tool(arg='val')")
    assert ('...' in format_tool_call_for_display('my_tool', {'arg': ('a' * 200)}))
    assert (format_tool_call_for_display(None, None, method='GET') == 'GET')

def test_format_tool_result_for_display():
    res = format_tool_result_for_display('my_tool', 'plain text')
    assert (res == "my_tool() -> 'plain text'")
    res = format_tool_result_for_display('my_tool', json.dumps({'content': [{'type': 'text', 'text': 'inner text'}]}))
    assert ('inner text' in res)
    res = format_tool_result_for_display('my_tool', 'res', args={'k': 'v'})
    assert (res == "my_tool(k='v') -> 'res'")

def test_update_activity_state():
    update_activity_state('phase1', round_num=1, tool_name='tool1')
    assert (_activity_state['phase'] == 'phase1')
    assert (_activity_state['round_num'] == 1)
    assert (_activity_state['tool_name'] == 'tool1')
    assert (_activity_state['last_activity'] > 0)

class TestLoggingErrorHandling():

    def test_safe_logger_success(self):
        mock_underlying = MagicMock()
        logger = SafeLogger(mock_underlying)
        logger.error('Test error', exc_info=True)
        logger.warning('Test warning')
        mock_underlying.error.assert_called_once_with('Test error', exc_info=True)
        mock_underlying.warning.assert_called_once_with('Test warning')

    def test_safe_logger_fallback(self, capsys):
        mock_underlying = MagicMock()
        mock_underlying.error.side_effect = Exception('Logger crashed')
        mock_underlying.warning.side_effect = Exception('Logger crashed')
        logger = SafeLogger(mock_underlying)
        logger.error('Should fallback')
        logger.warning('Should fallback warning')
        captured = capsys.readouterr()
        assert ('LOG ERROR FAILED: Should fallback' in captured.out)
        assert ('LOG WARNING FAILED: Should fallback warning' in captured.out)

    def test_safe_logger_disable_fallback(self, capsys):
        mock_underlying = MagicMock()
        mock_underlying.error.side_effect = Exception('Logger crashed')
        logger = SafeLogger(mock_underlying)
        logger.disable_fallback()
        logger.error('Should be silent')
        captured = capsys.readouterr()
        assert ('LOG ERROR FAILED' not in captured.out)

    def test_safe_log_exception_success(self):
        mock_logger = MagicMock()
        try:
            (1 / 0)
        except Exception as e:
            safe_log_exception(e, context='test_ctx', logger=mock_logger)
        mock_logger.error.assert_called_once()
        (args, kwargs) = mock_logger.error.call_args
        assert ('division by zero' in args[0])
        assert (kwargs['extra']['error_details']['context'] == 'test_ctx')

    def test_safe_log_exception_fallback(self, capsys):
        mock_logger = MagicMock()
        mock_logger.error.side_effect = Exception('Logger crashed')
        try:
            (1 / 0)
        except Exception as e:
            safe_log_exception(e, context='test_ctx', logger=mock_logger)
        captured = capsys.readouterr()
        assert ('CRITICAL: Logging failed for exception' in captured.out)

    def test_safe_log_exception_final_fallback(self, capsys):

        class BrokenLogger():

            @property
            def error(self):
                raise Exception('Fatal logger error')
        broken_logger = BrokenLogger()
        try:
            (1 / 0)
        except Exception as e:
            safe_log_exception(e, logger=broken_logger)
        captured = capsys.readouterr()
        assert ('CRITICAL: Logging failed for exception' in captured.out)
if __name__ == '__main__':
    unittest.main()
