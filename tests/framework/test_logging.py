
import unittest
import json
import logging
import os
import tempfile
from logging.handlers import MemoryHandler
from unittest.mock import MagicMock, patch

from plugin.framework.logging import (
    FLUSH_INTERVAL_SEC,
    LOG_REDACT_AUDIO_PLACEHOLDER,
    LOG_REDACT_IMAGE_PLACEHOLDER,
    OptionalFlushFileHandler,
    SafeLogger,
    _enable_agent_log,
    agent_log,
    format_tool_call_for_display,
    format_tool_result_for_display,
    get_debug_log_path,
    init_logging,
    redact_sensitive_payload_for_log,
    safe_log_exception,
    update_activity_state,
    _activity_state,
    log,
)

class TestInitLogging(unittest.TestCase):

    def setUp(self):
        import plugin.framework.config as config_mod
        import plugin.framework.logging as logging_mod

        self._saved_config_path = config_mod._resolved_config_path
        self._saved_debug_path = logging_mod._debug_log_path
        self._saved_hooks = logging_mod._exception_hooks_installed
        config_mod._resolved_config_path = None
        logging_mod._debug_log_path = None
        logging_mod._exception_hooks_installed = False
        for h in list(log.handlers):
            log.removeHandler(h)

    def tearDown(self):
        import plugin.framework.config as config_mod
        import plugin.framework.logging as logging_mod

        config_mod._resolved_config_path = self._saved_config_path
        logging_mod._debug_log_path = self._saved_debug_path
        logging_mod._exception_hooks_installed = self._saved_hooks
        for h in list(log.handlers):
            log.removeHandler(h)

    def test_init_logging_uses_ctx_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "writeragent.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("{}")

            mock_ctx = MagicMock()
            with (
                patch("plugin.framework.config._resolve_config_path_from_ctx", return_value=config_path),
                patch("plugin.framework.config.user_config_dir", return_value=tmp),
            ):
                init_logging(mock_ctx)

            expected_log = os.path.join(tmp, "writeragent_debug.log")
            self.assertEqual(get_debug_log_path(), expected_log)
            self.assertTrue(os.path.isfile(expected_log))
            with open(expected_log, encoding="utf-8") as fh:
                contents = fh.read()
            self.assertIn("Debug log active", contents)
            self.assertIn(expected_log, contents)


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


class TestAgentLog(unittest.TestCase):

    def setUp(self):
        import plugin.framework.logging as logging_mod
        self._saved_enable = logging_mod._enable_agent_log
        for h in list(log.handlers):
            log.removeHandler(h)

    def tearDown(self):
        import plugin.framework.logging as logging_mod
        logging_mod._enable_agent_log = self._saved_enable
        for h in list(log.handlers):
            log.removeHandler(h)

    def test_agent_log_writes_when_enabled(self):
        import plugin.framework.logging as logging_mod
        logging_mod._enable_agent_log = True
        handler = MemoryHandler(capacity=10)
        handler.setLevel(logging.DEBUG)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        agent_log("test.py:1", "hello", data={"k": "v"}, hypothesis_id="H1")
        handler.flush()
        assert len(handler.buffer) == 1
        record = handler.buffer[0]
        assert "[Agent]" in record.getMessage()
        payload = json.loads(record.getMessage().split("[Agent] ", 1)[1])
        assert payload["location"] == "test.py:1"
        assert payload["message"] == "hello"
        assert payload["data"] == {"k": "v"}
        assert payload["hypothesisId"] == "H1"

    def test_agent_log_noop_when_disabled(self):
        import plugin.framework.logging as logging_mod
        logging_mod._enable_agent_log = False
        handler = MemoryHandler(capacity=10)
        handler.setLevel(logging.DEBUG)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        agent_log("test.py:1", "hello")
        handler.flush()
        assert len(handler.buffer) == 0


class TestOptionalFlushFileHandler(unittest.TestCase):

    def setUp(self):
        import plugin.framework.logging as logging_mod
        logging_mod._debug_log_last_flush = 0.0

    def tearDown(self):
        import plugin.framework.logging as logging_mod
        logging_mod._debug_log_last_flush = 0.0

    def test_flush_rate_limited_within_interval(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
            path = tmp.name
        handler = OptionalFlushFileHandler(path, encoding="utf-8")
        try:
            with patch("plugin.framework.logging._monotonic", return_value=100.0):
                with patch.object(logging.FileHandler, "flush") as super_flush:
                    handler.flush()
                    handler.flush()
                    assert super_flush.call_count == 1
        finally:
            handler.close()

    def test_flush_allowed_after_interval(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
            path = tmp.name
        handler = OptionalFlushFileHandler(path, encoding="utf-8")
        try:
            with patch.object(logging.FileHandler, "flush") as super_flush:
                with patch("plugin.framework.logging._monotonic", return_value=100.0):
                    handler.flush()
                assert super_flush.call_count == 1
                with patch("plugin.framework.logging._monotonic", return_value=100.0 + FLUSH_INTERVAL_SEC):
                    handler.flush()
                assert super_flush.call_count == 2
        finally:
            handler.close()

    def test_close_flushes_unconditionally(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
            path = tmp.name
        handler = OptionalFlushFileHandler(path, encoding="utf-8")
        with patch("plugin.framework.logging._monotonic", return_value=100.0):
            with patch.object(logging.FileHandler, "flush") as super_flush:
                handler.flush()
                assert super_flush.call_count == 1
                handler.close()
                assert super_flush.call_count == 2


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
