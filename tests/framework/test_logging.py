
import io
import json
import logging
import os
from pathlib import Path
import tempfile
from logging.handlers import MemoryHandler
from unittest.mock import MagicMock, patch

import pytest
from typing import Any

from plugin.framework.logging import (
    FLUSH_INTERVAL_SEC,
    LOG_REDACT_AUDIO_PLACEHOLDER,
    LOG_REDACT_IMAGE_PLACEHOLDER,
    LOG_REDACT_SIGNATURE_MIN_LEN,
    LOG_REDACT_SIGNATURE_PLACEHOLDER,
    OptionalFlushFileHandler,
    agent_log,
    format_tool_call_for_display,
    format_tool_result_for_display,
    get_debug_log_path,
    init_logging,
    redact_sensitive_payload_for_log,
    resolve_log_level,
    update_activity_state,
    log,
)

class TestInitLogging:

    def setup_method(self):
        import sys

        import plugin.framework.config as config_mod
        import plugin.framework.logging as logging_mod

        self._saved_config_path = config_mod._resolved_config_path
        self._saved_debug_path = logging_mod._shared()["debug_log_path"]
        self._saved_hooks = getattr(sys, "_writeragent_exception_hooks_installed", False)
        self._saved_excepthook = sys.excepthook
        import threading

        self._saved_threading_excepthook = getattr(threading, "excepthook", None)
        self._saved_root_handlers = list(logging.getLogger().handlers)
        self._saved_last_resort = logging.lastResort
        self._saved_propagate = log.propagate
        plugin_logger = logging.getLogger("plugin")
        self._saved_plugin_propagate = plugin_logger.propagate
        self._saved_plugin_level = plugin_logger.level
        self._saved_plugin_handlers = list(plugin_logger.handlers)
        config_mod.reset_config_for_tests()
        config_mod._resolved_config_path = None
        logging_mod._shared()["debug_log_path"] = None
        # What was wrong: these lived on the logging module. A second import
        # did not see them. Tests must reset the sys copies or they leak.
        setattr(sys, "_writeragent_debug_file_handler", None)
        setattr(sys, "_writeragent_exception_hooks_installed", False)
        for h in list(log.handlers):
            log.removeHandler(h)

    def _release_debug_handlers(self) -> None:
        import sys

        import plugin.framework.logging as logging_mod

        for logger in (log, logging.getLogger(), logging.getLogger("plugin")):
            for handler in list(logger.handlers):
                try:
                    logger.removeHandler(handler)
                    handler.close()
                except Exception:
                    pass
        logging_mod._shared()["debug_log_path"] = None
        setattr(sys, "_writeragent_debug_file_handler", None)

    def teardown_method(self):
        import sys

        import plugin.framework.config as config_mod
        import plugin.framework.logging as logging_mod

        config_mod._resolved_config_path = self._saved_config_path
        logging_mod._shared()["debug_log_path"] = self._saved_debug_path
        setattr(sys, "_writeragent_debug_file_handler", None)
        setattr(sys, "_writeragent_exception_hooks_installed", self._saved_hooks)
        sys.excepthook = self._saved_excepthook
        import threading

        if self._saved_threading_excepthook is not None:
            threading.excepthook = self._saved_threading_excepthook
        logging.lastResort = self._saved_last_resort
        log.propagate = self._saved_propagate
        plugin_logger = logging.getLogger("plugin")
        plugin_logger.propagate = self._saved_plugin_propagate
        plugin_logger.setLevel(self._saved_plugin_level)
        for handler in list(plugin_logger.handlers):
            if handler not in self._saved_plugin_handlers:
                plugin_logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        for h in list(log.handlers):
            log.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in self._saved_root_handlers:
            root.addHandler(h)

    def test_init_logging_skips_missing_config_dir(self):
        mock_ctx = MagicMock()
        with (
            patch(
                "plugin.framework.config.user_config_dir",
                return_value="/no/such/MagicMock/user/config",
            ),
            patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            init_logging(mock_ctx)
        assert (get_debug_log_path()) is None
        assert ("file handler failed") not in (err.getvalue())
        assert ("MagicMock") not in (err.getvalue())

    def test_init_logging_uses_ctx_config_dir(self):
        # Windows can keep the FileHandler lock briefly after close(); ignore cleanup then.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
            assert (get_debug_log_path()) == (expected_log)
            assert (os.path.isfile(expected_log))
            with open(expected_log, encoding="utf-8") as fh:
                contents = fh.read()
            assert ("Debug log active") in (contents)
            assert (expected_log) in (contents)
            assert ("DIAG |") not in (contents)
            self._release_debug_handlers()

    def test_init_logging_strips_stray_console_handlers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = os.path.join(tmp, "writeragent.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("{}")

            root = logging.getLogger()
            root_buf = io.StringIO()
            root.addHandler(logging.StreamHandler(root_buf))
            wa_buf = io.StringIO()
            log.addHandler(logging.StreamHandler(wa_buf))
            module_logger = logging.getLogger("plugin.chatbot.send_handlers")
            module_stderr_buf = io.StringIO()
            module_logger.addHandler(logging.StreamHandler(module_stderr_buf))

            mock_ctx = MagicMock()
            with (
                patch("plugin.framework.config._resolve_config_path_from_ctx", return_value=config_path),
                patch("plugin.framework.config.user_config_dir", return_value=tmp),
            ):
                init_logging(mock_ctx)

            log.warning("writeragent-console-probe")
            module_logger.warning("module-console-probe")
            for handler in list(log.handlers) + list(root.handlers):
                if isinstance(handler, logging.FileHandler):
                    logging.FileHandler.flush(handler)

            expected_log = os.path.join(tmp, "writeragent_debug.log")
            with open(expected_log, encoding="utf-8") as fh:
                contents = fh.read()
            assert ("writeragent-console-probe") in (contents)
            assert ("module-console-probe") in (contents)
            assert (wa_buf.getvalue()) == ("")
            # writeragent does not propagate. plugin.* still does, so a root
            # handler sees module logs and not the writeragent logger.
            assert ("writeragent-console-probe") not in (root_buf.getvalue())
            assert ("module-console-probe") in (root_buf.getvalue())
            assert ("module-console-probe") in (module_stderr_buf.getvalue())
            self._release_debug_handlers()

    def test_init_logging_leaves_root_and_last_resort(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = os.path.join(tmp, "writeragent.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("{}")

            mock_ctx = MagicMock()
            with (
                patch("plugin.framework.config._resolve_config_path_from_ctx", return_value=config_path),
                patch("plugin.framework.config.user_config_dir", return_value=tmp),
            ):
                init_logging(mock_ctx)

            logging.getLogger("plugin.framework.file_probe").warning("plugin-file-probe")
            logging.getLogger("urllib3").warning("third-party-probe-url")
            handler = next(h for h in log.handlers if isinstance(h, logging.FileHandler))
            logging.FileHandler.flush(handler)
            with open(os.path.join(tmp, "writeragent_debug.log"), encoding="utf-8") as fh:
                contents = fh.read()
            assert "plugin-file-probe" in contents
            assert "third-party-probe-url" not in contents
            assert logging.lastResort is self._saved_last_resort
            assert not any(isinstance(h, OptionalFlushFileHandler) for h in logging.getLogger().handlers)
            plugin_handlers = [h for h in logging.getLogger("plugin").handlers if isinstance(h, logging.FileHandler)]
            assert plugin_handlers[0] is handler
            self._release_debug_handlers()

    def test_shared_handler_rotation_caps_backup(self):
        import plugin.framework.logging as logging_mod

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = os.path.join(tmp, "writeragent.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("{}")

            mock_ctx = MagicMock()
            with (
                patch("plugin.framework.config._resolve_config_path_from_ctx", return_value=config_path),
                patch("plugin.framework.config.user_config_dir", return_value=tmp),
            ):
                init_logging(mock_ctx)

            wa_handlers = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
            plugin_handlers = [h for h in logging.getLogger("plugin").handlers if isinstance(h, logging.FileHandler)]
            assert len(wa_handlers) == 1
            assert wa_handlers[0] is plugin_handlers[0]
            assert wa_handlers[0] not in logging.getLogger().handlers
            handler = wa_handlers[0]

            log.warning("A" * (logging_mod._MAX_DEBUG_LOG_BYTES + 64))
            logging.FileHandler.flush(handler)
            handler._rotate_if_large()

            expected_log = os.path.join(tmp, "writeragent_debug.log")
            backup = expected_log + ".1"
            assert os.path.isfile(backup)
            backup_size = os.path.getsize(backup)
            assert backup_size > logging_mod._MAX_DEBUG_LOG_BYTES

            logging.getLogger("plugin.framework.rotation_probe").warning("B" * 4096)
            logging.FileHandler.flush(handler)
            assert os.path.getsize(backup) == backup_size
            with open(expected_log, encoding="utf-8") as fh:
                live = fh.read()
            assert "B" * 64 in live
            self._release_debug_handlers()

    def test_reinit_keeps_path_when_config_dir_disappears(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = os.path.join(tmp, "writeragent.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"enable_agent_log": True, "log_level": "WARN"}, fh)

            mock_ctx = MagicMock()
            with (
                patch("plugin.framework.config._resolve_config_path_from_ctx", return_value=config_path),
                patch("plugin.framework.config.user_config_dir", return_value=tmp),
            ):
                init_logging(mock_ctx)

            expected_log = os.path.join(tmp, "writeragent_debug.log")
            assert get_debug_log_path() == expected_log
            import plugin.framework.logging as logging_mod

            shared = logging_mod._shared()
            assert shared["enable_agent_log"] is True
            first_handler = logging_mod._debug_file_handler_from_sys()
            assert first_handler is not None
            assert first_handler in log.handlers
            assert log.propagate is False

            # Second init_logging with user_config_dir patched to a missing directory
            with patch("plugin.framework.config.user_config_dir", return_value=os.path.join(tmp, "missing")):
                init_logging(mock_ctx)

            assert get_debug_log_path() == expected_log
            assert shared["enable_agent_log"] is True
            assert logging_mod._debug_file_handler_from_sys() is first_handler
            assert first_handler in log.handlers
            assert log.propagate is False

            # Third call with user_config_dir raising ConfigError
            from plugin.framework.errors import ConfigError

            with patch("plugin.framework.config.user_config_dir", side_effect=ConfigError("config disappeared")):
                init_logging(mock_ctx)

            assert get_debug_log_path() == expected_log
            assert shared["enable_agent_log"] is True
            assert logging_mod._debug_file_handler_from_sys() is first_handler
            assert first_handler in log.handlers
            assert log.propagate is False

            self._release_debug_handlers()



class TestLogRedaction:

    def test_redact_chat_multimodal(self) -> None:
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}, {'type': 'input_audio', 'input_audio': {'data': 'AAAABBBB', 'format': 'wav'}}, {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,ZZZZ'}}]}]
        out = redact_sensitive_payload_for_log(messages)
        assert (out) is not (messages)
        parts = out[0]['content']
        assert (parts[0]) == ({'type': 'text', 'text': 'hi'})
        assert (parts[1]['input_audio']['data']) == (LOG_REDACT_AUDIO_PLACEHOLDER % 8)
        assert (parts[2]['image_url']['url']) == (LOG_REDACT_IMAGE_PLACEHOLDER % len('data:image/jpeg;base64,ZZZZ'))
        assert (messages[0]['content'][1]['input_audio']['data']) == ('AAAABBBB')

    def test_redact_image_generations_request_body(self) -> None:
        u = ('data:image/png;base64,' + ('x' * 100))
        data = {'prompt': 'p', 'image_url': u}
        r = redact_sensitive_payload_for_log(data)
        assert (r['prompt']) == ('p')
        assert (r['image_url']) == (LOG_REDACT_IMAGE_PLACEHOLDER % len(u))

    def test_redact_image_api_response_nested(self) -> None:
        raw = {'data': [{'b64_json': 'Ym9n', 'url': 'http://ok'}, {'url': 'data:image/png;base64,QQ=='}]}
        r = redact_sensitive_payload_for_log(raw)
        assert (r['data'][0]['b64_json']) == (LOG_REDACT_IMAGE_PLACEHOLDER % 4)
        assert (r['data'][0]['url']) == ('http://ok')
        assert (r['data'][1]['url']) == (LOG_REDACT_IMAGE_PLACEHOLDER % len('data:image/png;base64,QQ=='))

    def test_redact_long_reasoning_details_signature(self) -> None:
        long_sig = "S" * (LOG_REDACT_SIGNATURE_MIN_LEN + 100)
        short_sig = "sig-abc"
        raw = {
            "id": "gen-1",
            "model": "google/gemini-2.5-flash-image",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "hello",
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "think", "signature": long_sig},
                            {"type": "reasoning.text", "text": "short", "signature": short_sig},
                        ],
                    },
                }
            ],
        }
        out = redact_sensitive_payload_for_log(raw)
        assert (out) is not (raw)
        details = out["choices"][0]["message"]["reasoning_details"]
        assert (details[0]["signature"]) == (LOG_REDACT_SIGNATURE_PLACEHOLDER % len(long_sig))
        assert (details[1]["signature"]) == (short_sig)
        assert (raw["choices"][0]["message"]["reasoning_details"][0]["signature"]) == (long_sig)
        assert (out["id"]) == ("gen-1")
        assert (out["model"]) == ("google/gemini-2.5-flash-image")
        assert (out["choices"][0]["finish_reason"]) == ("stop")
        assert (out["choices"][0]["message"]["content"]) == ("hello")

    def test_redact_signature_threshold_boundary(self) -> None:
        just_under = "a" * (LOG_REDACT_SIGNATURE_MIN_LEN - 1)
        at_min = "b" * LOG_REDACT_SIGNATURE_MIN_LEN
        raw = {"signature": just_under, "nested": {"signature": at_min}}
        out = redact_sensitive_payload_for_log(raw)
        assert (out["signature"]) == (just_under)
        assert (out["nested"]["signature"]) == (LOG_REDACT_SIGNATURE_PLACEHOLDER % LOG_REDACT_SIGNATURE_MIN_LEN)
        assert (raw["nested"]["signature"]) == (at_min)

def test_resolve_log_level_allowlist():
    assert resolve_log_level("DEBUG") == logging.DEBUG
    assert resolve_log_level("warn") == logging.WARNING
    assert resolve_log_level("WARN") == logging.WARNING
    assert resolve_log_level("__dict__") == logging.WARNING
    assert resolve_log_level(None) == logging.WARNING


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

@pytest.fixture(autouse=True)
def _no_stale_watchdog_status_control():
    # Other modules' _do_send tests (same xdist worker) leave a mock sender
    # control in _activity_state; the watchdog would then label that one.
    import plugin.framework.logging as logging_mod

    shared = logging_mod._shared()
    with shared["activity_lock"]:
        shared["activity"]["status_control"] = None
    yield


def test_update_activity_state():
    import plugin.framework.logging as logging_mod

    update_activity_state('phase1', round_num=1, tool_name='tool1')
    act = logging_mod._shared()['activity']
    assert (act['phase'] == 'phase1')
    assert (act['round_num'] == 1)
    assert (act['tool_name'] == 'tool1')
    assert (act['last_activity'] > 0)


def test_watchdog_clears_hung_status_when_activity_resumes():
    import plugin.framework.logging as logging_mod

    class Ctrl:
        def __init__(self) -> None:
            self.text = "Working"

        def getText(self) -> str:
            return self.text

        def setText(self, value: str) -> None:
            self.text = value

    ctrl = Ctrl()

    def immediate(fn, *args, **kwargs):
        fn(*args, **kwargs)

    shared = logging_mod._shared()
    saved = shared["hung_shown"]
    try:
        shared["hung_shown"] = False
        update_activity_state("chat", round_num=2, tool_name="edit")
        with shared["activity_lock"]:
            shared["activity"]["last_activity"] = 0.0
        with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=immediate):
            logging_mod._watchdog_check(ctrl)
            assert ctrl.text.startswith("Hung:")
            update_activity_state("chat", round_num=2)
            logging_mod._watchdog_check(ctrl)
            assert ctrl.text == ""
            ctrl.text = "Ready"
            shared["hung_shown"] = True
            logging_mod._watchdog_check(ctrl)
            assert ctrl.text == "Ready"
    finally:
        shared["hung_shown"] = saved
        update_activity_state("")


class TestAgentLog:

    def setup_method(self):
        import plugin.framework.logging as logging_mod
        self._saved_enable = logging_mod._shared()["enable_agent_log"]
        for h in list(log.handlers):
            log.removeHandler(h)

    def teardown_method(self):
        import plugin.framework.logging as logging_mod
        logging_mod._shared()["enable_agent_log"] = self._saved_enable
        for h in list(log.handlers):
            log.removeHandler(h)

    def test_agent_log_writes_when_enabled(self):
        import plugin.framework.logging as logging_mod
        logging_mod._shared()["enable_agent_log"] = True
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

    def test_agent_log_redacts_secret_keys(self):
        import plugin.framework.logging as logging_mod
        logging_mod._shared()["enable_agent_log"] = True
        handler = MemoryHandler(capacity=10)
        handler.setLevel(logging.DEBUG)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        original = {"api_key": "sk-live", "nested": {"Authorization": "Bearer secret"}}
        agent_log("test.py:2", "secrets", data=original)
        assert original["api_key"] == "sk-live"
        payload = json.loads(handler.buffer[0].getMessage().split("[Agent] ", 1)[1])
        assert payload["data"]["api_key"] == "<redacted>"
        assert payload["data"]["nested"]["Authorization"] == "<redacted>"

    def test_tool_display_redacts_secret_args(self):
        shown = format_tool_call_for_display("fetch", {"api_key": "sk-live", "q": "cats"})
        assert "sk-live" not in shown
        assert "<redacted>" in shown
        assert "cats" in shown
        nested = format_tool_call_for_display(
            "fetch",
            {"api_keys_by_endpoint": {"https://api.example": "sk-nested"}, "x-api-key": "sk-header"},
        )
        assert "sk-nested" not in nested
        assert "sk-header" not in nested

    def test_agent_log_noop_when_disabled(self):
        import plugin.framework.logging as logging_mod
        logging_mod._shared()["enable_agent_log"] = False
        handler = MemoryHandler(capacity=10)
        handler.setLevel(logging.DEBUG)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        agent_log("test.py:1", "hello")
        handler.flush()
        assert len(handler.buffer) == 0

    def test_agent_log_does_not_raise_on_lock(self):
        import threading

        import plugin.framework.logging as logging_mod
        logging_mod._shared()["enable_agent_log"] = True
        handler = MemoryHandler(capacity=10)
        handler.setLevel(logging.DEBUG)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        agent_log("test.py:3", "lock", data={"lock": threading.Lock()})
        handler.flush()
        assert len(handler.buffer) == 0

    def test_agent_log_emits_when_parent_level_is_warning(self):
        import plugin.framework.logging as logging_mod
        logging_mod._shared()["enable_agent_log"] = True
        handler = MemoryHandler(capacity=10)
        handler.setLevel(logging.DEBUG)
        log.addHandler(handler)
        log.setLevel(logging.WARNING)
        log.debug("ordinary-debug")
        handler.flush()
        assert len(handler.buffer) == 0

        agent_log("test.py:1", "hello")
        handler.flush()
        assert len(handler.buffer) == 1
        record = handler.buffer[0]
        assert "[Agent]" in record.getMessage()
        assert "hello" in record.getMessage()
        assert record.name == "writeragent.agent"


def test_release_strip_keeps_agent_log_sink(tmp_path: Path) -> None:
    from scripts.strip_code import strip_log_debug_info_calls

    source_path = Path("plugin/framework/logging.py")
    source_content = source_path.read_text(encoding="utf-8")
    temp_target = tmp_path / "plugin" / "framework" / "logging.py"
    temp_target.parent.mkdir(parents=True, exist_ok=True)
    temp_target.write_text(source_content, encoding="utf-8")

    strip_log_debug_info_calls(str(tmp_path))

    stripped_content = temp_target.read_text(encoding="utf-8")
    assert "_agent_logger.log(" in stripped_content
    assert "_agent_logger.debug(" not in stripped_content


class TestOptionalFlushFileHandler:

    def setup_method(self):
        import plugin.framework.logging as logging_mod
        logging_mod._debug_log_last_flush = 0.0

    def teardown_method(self):
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

    def test_emit_warning_forces_flush_within_interval(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
            path = tmp.name
        handler = OptionalFlushFileHandler(path, encoding="utf-8")
        try:
            with patch("plugin.framework.logging._monotonic", return_value=100.0):
                with patch.object(logging.FileHandler, "flush") as super_flush:
                    # Initial flush consumes rate limit
                    handler.flush()
                    assert super_flush.call_count == 1

                    # Emitting INFO within interval does not flush
                    info_record = logging.LogRecord("test", logging.INFO, "test.py", 1, "info msg", (), None)
                    handler.emit(info_record)
                    assert super_flush.call_count == 1

                    # Emitting WARNING within interval forces flush
                    warn_record = logging.LogRecord("test", logging.WARNING, "test.py", 2, "warn msg", (), None)
                    handler.emit(warn_record)
                    assert super_flush.call_count == 2
        finally:
            handler.close()

    def test_watchdog_check_flushes_debug_log(self):
        from plugin.framework.logging import _watchdog_check
        import sys

        import plugin.framework.logging as logging_mod

        mock_handler = MagicMock(spec=OptionalFlushFileHandler)
        old_h = getattr(sys, "_writeragent_debug_file_handler", None)
        try:
            setattr(sys, "_writeragent_debug_file_handler", mock_handler)
            # Idle: no phase. A stalled phase left by an earlier test would
            # also run the once-per-stall stack dump, which flushes again.
            with patch.dict(logging_mod._shared()["activity"], {"phase": ""}):
                _watchdog_check(None)
            mock_handler.flush.assert_called_once_with(force=True)
        finally:
            setattr(sys, "_writeragent_debug_file_handler", old_h)


def test_redact_api_keys_by_endpoint_and_exact_secret_names() -> None:
    from plugin.framework.logging import LOG_REDACT_SECRET_PLACEHOLDER

    raw = {
        "api_keys_by_endpoint": {"https://api.example": "sk-live", "count": 1},
        "x-api-key": "sk-header",
        "token": "abc",
        "Secret": "xyz",
        "max_tokens": 128,
        "secret_sauce": "keep",
        "client_secret": "cs-secret",
        "private_key": "pk-secret",
        "session_token": "st-secret",
        "apikey": "ak-secret",
        "ApiKey": "ak-secret2",
        "auth": "visible",
        "token_int": {"token": 99},
    }
    out = redact_sensitive_payload_for_log(raw)
    assert out["api_keys_by_endpoint"]["https://api.example"] == LOG_REDACT_SECRET_PLACEHOLDER
    assert out["api_keys_by_endpoint"]["count"] == 1
    assert out["x-api-key"] == LOG_REDACT_SECRET_PLACEHOLDER
    assert out["token"] == LOG_REDACT_SECRET_PLACEHOLDER
    assert out["Secret"] == LOG_REDACT_SECRET_PLACEHOLDER
    assert out["max_tokens"] == 128
    assert out["secret_sauce"] == "keep"
    assert out["client_secret"] == LOG_REDACT_SECRET_PLACEHOLDER
    assert out["private_key"] == LOG_REDACT_SECRET_PLACEHOLDER
    assert out["session_token"] == LOG_REDACT_SECRET_PLACEHOLDER
    assert out["apikey"] == LOG_REDACT_SECRET_PLACEHOLDER
    assert out["ApiKey"] == LOG_REDACT_SECRET_PLACEHOLDER
    assert out["auth"] == "visible"
    assert out["token_int"]["token"] == 99
    assert raw["api_keys_by_endpoint"]["https://api.example"] == "sk-live"
    assert raw["token"] == "abc"


def test_excepthook_is_not_wrapped_twice() -> None:
    import sys
    import threading

    import plugin.framework.logging as logging_mod

    saved_hook = sys.excepthook
    saved_thread = getattr(threading, "excepthook", None)
    saved_flag = getattr(sys, "_writeragent_exception_hooks_installed", False)
    try:
        def sentinel(*_args):
            return None

        sys.excepthook = sentinel
        setattr(sys, "_writeragent_exception_hooks_installed", False)
        logging_mod._install_global_exception_hooks()
        first = sys.excepthook
        assert getattr(first, "_writeragent_hook", False) is True
        assert first is not sentinel
        setattr(sys, "_writeragent_exception_hooks_installed", False)
        logging_mod._install_global_exception_hooks()
        assert sys.excepthook is first
    finally:
        sys.excepthook = saved_hook
        if saved_thread is not None:
            threading.excepthook = saved_thread
        setattr(sys, "_writeragent_exception_hooks_installed", saved_flag)


def test_log_record_factory_is_not_wrapped_twice() -> None:
    import plugin.framework.logging as logging_mod

    first = logging.getLogRecordFactory()
    logging_mod._install_safe_log_record_factory()
    second = logging.getLogRecordFactory()
    assert second is first
    assert getattr(second, "_writeragent_safe_factory", False) is True


def test_debug_handler_closes_previous_when_path_changes(tmp_path) -> None:
    import sys

    import plugin.framework.logging as logging_mod

    path_a = str(tmp_path / "a.log")
    path_b = str(tmp_path / "b.log")
    saved_path = logging_mod._shared()["debug_log_path"]
    saved_handler = getattr(sys, "_writeragent_debug_file_handler", None)
    first = None
    second = None
    try:
        setattr(sys, "_writeragent_debug_file_handler", None)
        logging_mod._shared()["debug_log_path"] = path_a
        first = logging_mod._shared_debug_file_handler()
        closed: list[int] = []
        original_close = first.close

        def tracking_close() -> None:
            closed.append(1)
            original_close()

        with patch.object(first, "close", tracking_close):
            logging_mod._shared()["debug_log_path"] = path_b
            second = logging_mod._shared_debug_file_handler()
            assert closed == [1]
            assert second is not first
            assert getattr(sys, "_writeragent_debug_file_handler") is second
            assert logging_mod._shared_debug_file_handler() is second
            assert closed == [1]
    finally:
        logging_mod._shared()["debug_log_path"] = saved_path
        for handler in (second, first):
            if handler is None:
                continue
            try:
                logging.FileHandler.close(handler)
            except Exception:
                pass
        setattr(sys, "_writeragent_debug_file_handler", saved_handler)


def test_debug_handler_is_stored_on_sys(tmp_path) -> None:
    import sys

    import plugin.framework.logging as logging_mod

    path = str(tmp_path / "writeragent_debug.log")
    saved_path = logging_mod._shared()["debug_log_path"]
    saved_handler = getattr(sys, "_writeragent_debug_file_handler", None)
    handler = None
    try:
        logging_mod._shared()["debug_log_path"] = path
        setattr(sys, "_writeragent_debug_file_handler", None)
        handler = logging_mod._shared_debug_file_handler()
        assert getattr(sys, "_writeragent_debug_file_handler") is handler
        assert logging_mod._shared_debug_file_handler() is handler
    finally:
        logging_mod._shared()["debug_log_path"] = saved_path
        if handler is not None:
            handler.close()
        setattr(sys, "_writeragent_debug_file_handler", saved_handler)


def test_start_watchdog_is_dedicated_and_idempotent() -> None:
    import sys

    import plugin.framework.logging as logging_mod

    saved = getattr(sys, "_writeragent_watchdog_started", False)
    try:
        setattr(sys, "_writeragent_watchdog_started", False)
        with patch("plugin.framework.worker_pool.run_in_background") as run:
            logging_mod.start_watchdog_thread(None, status_control=None)
            logging_mod.start_watchdog_thread(None, status_control=None)
        assert run.call_count == 1
        assert run.call_args.kwargs.get("dedicated") is True
        assert getattr(sys, "_writeragent_watchdog_started") is True
    finally:
        setattr(sys, "_writeragent_watchdog_started", saved)


def test_watchdog_dumps_thread_stacks_on_stall() -> None:
    """Watchdog dumps thread stacks once per stall episode, and dumps again on a new stall."""
    import threading

    import plugin.framework.logging as logging_mod

    captured_messages: list[str] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_messages.append(record.getMessage())

    handler = CaptureHandler()
    handler.setLevel(logging.DEBUG)
    logging_mod.log.addHandler(handler)

    worker_started = threading.Event()
    worker_stop = threading.Event()

    def worker_func() -> None:
        worker_started.set()
        worker_stop.wait(timeout=5.0)

    worker_thread = threading.Thread(
        target=worker_func,
        name="test-stall-worker-thread",
        daemon=True,
    )
    worker_thread.start()
    assert worker_started.wait(timeout=2.0)

    shared = logging_mod._shared()
    saved_hung = shared["hung_shown"]
    # The dump is DEBUG. Run alone, the logger is still at its default level.
    saved_level = logging_mod.log.level
    logging_mod.log.setLevel(logging.DEBUG)
    try:
        shared["hung_shown"] = False
        shared["stacks_dumped"] = False
        update_activity_state("chat", round_num=1)
        with shared["activity_lock"]:
            shared["activity"]["last_activity"] = 0.0

        # Call _watchdog_check several times during the first stall episode
        for idx in range(3):
            logging_mod._watchdog_check(None)

        # Assert exactly one stack dump was logged during this stall
        dumps = [m for m in captured_messages if "WATCHDOG: thread stack dump" in m]
        assert len(dumps) == 1

        dump = dumps[0]
        current_thread_name = threading.current_thread().name
        assert current_thread_name in dump
        assert "test-stall-worker-thread" in dump
        assert f"ident={worker_thread.ident}" in dump
        assert "daemon=True" in dump

        # Simulate activity resuming (last_activity updated, elapsed < threshold)
        update_activity_state("chat", round_num=2)
        logging_mod._watchdog_check(None)

        # Confirm no additional dump occurred when activity resumed
        dumps_after_resume = [m for m in captured_messages if "WATCHDOG: thread stack dump" in m]
        assert len(dumps_after_resume) == 1

        # Simulate a second stall episode
        with shared["activity_lock"]:
            shared["activity"]["last_activity"] = 0.0

        for idx in range(3):
            logging_mod._watchdog_check(None)

        # Assert a second dump was logged for the new stall episode
        dumps_after_second_stall = [m for m in captured_messages if "WATCHDOG: thread stack dump" in m]
        assert len(dumps_after_second_stall) == 2

        second_dump = dumps_after_second_stall[1]
        assert current_thread_name in second_dump
        assert "test-stall-worker-thread" in second_dump
    finally:
        logging_mod.log.setLevel(saved_level)
        worker_stop.set()
        worker_thread.join(timeout=2.0)
        shared["hung_shown"] = saved_hung
        update_activity_state("")
        logging_mod.log.removeHandler(handler)


def test_watchdog_dump_thread_stacks_never_raises() -> None:
    """If inspecting frames fails, watchdog check catches the error and does not raise."""
    import plugin.framework.logging as logging_mod

    shared = logging_mod._shared()
    saved_hung = shared["hung_shown"]
    try:
        shared["hung_shown"] = False
        update_activity_state("chat", round_num=1)
        with shared["activity_lock"]:
            shared["activity"]["last_activity"] = 0.0

        with patch("sys._current_frames", side_effect=RuntimeError("simulated frame inspection error")):
            # Must not raise
            logging_mod._watchdog_check(None)
    finally:
        shared["hung_shown"] = saved_hung
        update_activity_state("")


def test_watchdog_note_activity_resets_elapsed():
    import plugin.framework.logging as logging_mod
    import time

    shared = logging_mod._shared()
    logging_mod.update_activity_state("chat", round_num=1)

    with shared["activity_lock"]:
        shared["activity"]["last_activity"] = time.monotonic() - 40.0

    logging_mod.note_activity()

    with shared["activity_lock"]:
        elapsed = time.monotonic() - shared["activity"]["last_activity"]
        assert elapsed < 10.0


def test_watchdog_hung_goes_to_sender_control():
    import plugin.framework.logging as logging_mod
    import time

    class MockCtrl:
        def __init__(self):
            self.text = ""
        def setText(self, text):
            self.text = text

    sender_ctrl = MockCtrl()
    default_ctrl = MockCtrl()

    shared = logging_mod._shared()
    saved_hung = shared["hung_shown"]
    try:
        shared["hung_shown"] = False
        logging_mod.update_activity_state("do_send", status_control=sender_ctrl)

        with shared["activity_lock"]:
            shared["activity"]["last_activity"] = time.monotonic() - 40.0

        logging_mod._watchdog_check(default_ctrl)

        from plugin.framework.queue_executor import default_executor
        while default_executor.pending_work_count() > 0:
            default_executor.process_queue()

        assert "Hung" in sender_ctrl.text
        assert default_ctrl.text == ""
    finally:
        shared["hung_shown"] = saved_hung
        logging_mod.update_activity_state("")


def test_watchdog_ending_turn_clears_hung():
    import plugin.framework.logging as logging_mod

    class MockCtrl:
        def __init__(self):
            self.text = "Hung: do_send"
        def getText(self):
            return self.text
        def setText(self, text):
            self.text = text

    sender_ctrl = MockCtrl()

    logging_mod.update_activity_state("do_send", status_control=sender_ctrl)

    logging_mod.update_activity_state("")

    from plugin.framework.queue_executor import default_executor
    while default_executor.pending_work_count() > 0:
        default_executor.process_queue()

    assert sender_ctrl.text == ""


def test_update_activity_state_clears_omitted_tool_name():
    import plugin.framework.logging as logging_mod

    class Ctrl:
        def __init__(self) -> None:
            self.text = ""

        def getText(self) -> str:
            return self.text

        def setText(self, value: str) -> None:
            self.text = value

    ctrl = Ctrl()

    def immediate(fn, *args, **kwargs):
        fn(*args, **kwargs)

    shared = logging_mod._shared()
    saved_hung = shared["hung_shown"]
    try:
        shared["hung_shown"] = False
        update_activity_state("tool_execute", round_num=1, tool_name="edit")
        update_activity_state("tool_loop", round_num=2)
        act = shared["activity"]
        assert act["tool_name"] is None
        assert act["round_num"] == 2

        update_activity_state("exhausted_rounds")
        assert act["round_num"] == 2
        assert act["tool_name"] is None

        update_activity_state("tool_loop", round_num=2)
        with shared["activity_lock"]:
            act["last_activity"] = 0.0

        with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=immediate):
            logging_mod._watchdog_check(ctrl)

        assert ctrl.text == "Hung: tool_loop round 2"
        assert "edit" not in ctrl.text
    finally:
        shared["hung_shown"] = saved_hung
        update_activity_state("")


def test_redact_anthropic_and_google_image_blocks() -> None:
    anthropic_b64 = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    anthropic_url = {"type": "image", "source": {"type": "url", "url": "https://example.test/a.png"}}
    google_inline = {"inlineData": {"mimeType": "image/png", "data": "BBBB"}}
    plain_data = {"data": "not-an-image"}

    payload = [anthropic_b64, anthropic_url, google_inline, plain_data]
    out = redact_sensitive_payload_for_log(payload)

    assert out is not payload
    assert out[0]["source"]["data"] == LOG_REDACT_IMAGE_PLACEHOLDER % 4
    assert anthropic_b64["source"]["data"] == "AAAA"

    assert out[1] == {"type": "image", "source": {"type": "url", "url": "https://example.test/a.png"}}
    assert anthropic_url["source"]["url"] == "https://example.test/a.png"

    assert out[2]["inlineData"]["data"] == LOG_REDACT_IMAGE_PLACEHOLDER % 4
    assert google_inline["inlineData"]["data"] == "BBBB"

    assert out[3] == {"data": "not-an-image"}


def test_second_import_shares_state_and_handler(tmp_path) -> None:
    import importlib.util
    import sys
    import plugin.framework.logging as logging_mod

    path = str(tmp_path / "writeragent_debug.log")
    saved_path = logging_mod._shared()["debug_log_path"]
    saved_handler = getattr(sys, "_writeragent_debug_file_handler", None)
    logging_mod._shared()["debug_log_path"] = path
    setattr(sys, "_writeragent_debug_file_handler", None)
    first_handler = logging_mod._shared_debug_file_handler()
    try:
        spec = importlib.util.spec_from_file_location("writeragent_logging_second_copy", logging_mod.__file__)
        assert spec is not None and spec.loader is not None
        second = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(second)

        assert second._shared() is logging_mod._shared()
        second.update_activity_state("from-second", round_num=4, tool_name="edit")
        act = logging_mod._shared()["activity"]
        assert act["phase"] == "from-second"
        assert act["round_num"] == 4
        assert act["tool_name"] == "edit"

        assert second._shared_debug_file_handler() is getattr(sys, "_writeragent_debug_file_handler")
        assert second._shared_debug_file_handler() is first_handler
    finally:
        update_activity_state("")
        logging_mod._shared()["debug_log_path"] = saved_path
        if first_handler is not None:
            first_handler.close()
        setattr(sys, "_writeragent_debug_file_handler", saved_handler)


def test_rotate_if_large_second_thread_keeps_oversized_backup(tmp_path) -> None:
    import threading
    import plugin.framework.logging as logging_mod

    log_path = str(tmp_path / "test_rotate.log")
    handler = OptionalFlushFileHandler(log_path, encoding="utf-8")
    try:
        # Write more than _MAX_DEBUG_LOG_BYTES and flush so bytes are on disk
        oversized_data = "X" * (logging_mod._MAX_DEBUG_LOG_BYTES + 4096)
        handler.stream.write(oversized_data)
        handler.stream.flush()
        pre_size = os.path.getsize(log_path)
        assert pre_size > logging_mod._MAX_DEBUG_LOG_BYTES

        real_getsize = os.path.getsize
        entered = 0
        entered_lock = threading.Lock()
        both_entered = threading.Event()

        def sync_getsize(path: str) -> int:
            nonlocal entered
            size = real_getsize(path)
            with entered_lock:
                entered += 1
                count = entered
            if count <= 2:
                if count == 2:
                    both_entered.set()
                both_entered.wait(timeout=5.0)
            return size

        replace_count = 0
        real_replace = os.replace

        def counting_replace(src: str, dst: str) -> None:
            nonlocal replace_count
            replace_count += 1
            real_replace(src, dst)

        with (
            patch("plugin.framework.logging.os.path.getsize", side_effect=sync_getsize),
            patch("plugin.framework.logging.os.replace", side_effect=counting_replace),
        ):
            t1 = threading.Thread(target=handler._rotate_if_large)
            t2 = threading.Thread(target=handler._rotate_if_large)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

        backup = log_path + ".1"
        assert replace_count == 1
        assert os.path.isfile(backup)
        backup_size = os.path.getsize(backup)
        assert backup_size > logging_mod._MAX_DEBUG_LOG_BYTES
        assert backup_size == pre_size
        assert os.path.isfile(log_path)

        # One more _rotate_if_large on the small live file does not change .1's size
        handler._rotate_if_large()
        assert os.path.getsize(backup) == backup_size
    finally:
        handler.close()


def test_close_swallows_overridden_flush_error(tmp_path) -> None:
    log_path = str(tmp_path / "test_close.log")
    handler = OptionalFlushFileHandler(log_path, encoding="utf-8")

    def broken_flush(force: bool = False) -> None:
        raise OSError("disk failure")

    handler.flush = broken_flush  # type: ignore[assignment]
    handler.close()


def test_shared_concurrent_first_init_returns_one_box() -> None:
    import sys
    import threading
    import plugin.framework.logging as logging_mod

    saved = getattr(sys, "_writeragent_logging_shared", None)
    if hasattr(sys, "_writeragent_logging_shared"):
        delattr(sys, "_writeragent_logging_shared")
    try:
        barrier = threading.Barrier(8)
        results: list[dict[str, Any] | None] = [None] * 8

        def worker(idx: int) -> None:
            barrier.wait(timeout=5.0)
            results[idx] = logging_mod._shared()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        first_box = results[0]
        assert first_box is not None
        for box in results:
            assert box is first_box
        assert isinstance(first_box["init_lock"], type(threading.Lock()))
        assert isinstance(first_box["activity_lock"], type(threading.Lock()))
        assert logging_mod._shared() is first_box
    finally:
        setattr(sys, "_writeragent_logging_shared", saved)


def test_flush_swallows_stream_error(tmp_path) -> None:
    log_path = str(tmp_path / "test_flush.log")
    handler = OptionalFlushFileHandler(log_path, encoding="utf-8")
    try:
        with patch("logging.FileHandler.flush", side_effect=OSError("disk full")):
            handler.flush(force=True)
            handler.flush(force=False)
    finally:
        handler.close()


def test_flush_reraises_recursion_error(tmp_path) -> None:
    log_path = str(tmp_path / "test_flush_rec.log")
    handler = OptionalFlushFileHandler(log_path, encoding="utf-8")
    try:
        with patch("logging.FileHandler.flush", side_effect=RecursionError):
            with pytest.raises(RecursionError):
                handler.flush(force=True)
    finally:
        handler.close()


def test_rotate_reopen_oserror_does_not_propagate(tmp_path) -> None:
    import plugin.framework.logging as logging_mod

    log_path = str(tmp_path / "test_rotate_reopen.log")
    handler = OptionalFlushFileHandler(log_path, encoding="utf-8")
    try:
        oversized = "A" * (logging_mod._MAX_DEBUG_LOG_BYTES + 1024)
        assert handler.stream is not None
        handler.stream.write(oversized)
        logging.FileHandler.flush(handler)

        with patch.object(OptionalFlushFileHandler, "_open", side_effect=OSError("reopen failed")):
            handler._rotate_if_large()

        assert handler.stream is None
        backup = log_path + ".1"
        assert os.path.isfile(backup)
    finally:
        if handler.stream is not None:
            handler.close()


def test_watchdog_check_continues_when_flush_raises(tmp_path) -> None:
    import sys
    import plugin.framework.logging as logging_mod

    shared = logging_mod._shared()
    with shared["activity_lock"]:
        saved_activity = dict(shared["activity"])
        saved_hung = shared["hung_shown"]
        saved_stacks = shared["stacks_dumped"]
    saved_handler = getattr(sys, "_writeragent_debug_file_handler", None)

    log_path = str(tmp_path / "test_watchdog_check.log")
    handler = OptionalFlushFileHandler(log_path, encoding="utf-8")
    setattr(sys, "_writeragent_debug_file_handler", handler)

    try:
        with patch("logging.FileHandler.flush", side_effect=OSError("flush failed")):
            logging_mod.update_activity_state("chat", round_num=1)
            with shared["activity_lock"]:
                shared["activity"]["last_activity"] = 0.0
                shared["stacks_dumped"] = False

            logging_mod._watchdog_check(None)
            assert shared["stacks_dumped"] is True
    finally:
        logging_mod.update_activity_state("")
        with shared["activity_lock"]:
            shared["activity"].clear()
            shared["activity"].update(saved_activity)
            shared["hung_shown"] = saved_hung
            shared["stacks_dumped"] = saved_stacks
        setattr(sys, "_writeragent_debug_file_handler", saved_handler)
        handler.close()


def test_watchdog_loop_continues_after_tick_error() -> None:
    import threading
    import plugin.framework.logging as logging_mod

    stop_event = threading.Event()
    check_count = 0

    def mock_check(status_control: Any) -> None:
        nonlocal check_count
        check_count += 1
        if check_count == 1:
            raise OSError("disk full")
        stop_event.set()
        raise SystemExit

    with (
        patch("plugin.framework.logging.time.sleep", return_value=None),
        patch("plugin.framework.logging._watchdog_check", side_effect=mock_check),
        patch.object(logging_mod.log, "exception"),
    ):
        t = threading.Thread(target=logging_mod._watchdog_loop, args=(None,), daemon=True)
        t.start()
        assert stop_event.wait(timeout=2.0)
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert check_count >= 2


def test_watchdog_loop_continues_when_tick_log_fails() -> None:
    import threading
    import plugin.framework.logging as logging_mod

    stop_event = threading.Event()
    check_count = 0

    def mock_check(status_control: Any) -> None:
        nonlocal check_count
        check_count += 1
        if check_count == 1:
            raise OSError("disk full")
        stop_event.set()
        raise SystemExit

    with (
        patch("plugin.framework.logging.time.sleep", return_value=None),
        patch("plugin.framework.logging._watchdog_check", side_effect=mock_check),
        patch.object(logging_mod.log, "exception", side_effect=RuntimeError("log failed")),
    ):
        t = threading.Thread(target=logging_mod._watchdog_loop, args=(None,), daemon=True)
        t.start()
        assert stop_event.wait(timeout=2.0)
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert check_count >= 2


def test_start_watchdog_resets_flag_when_background_start_fails() -> None:
    import sys
    import plugin.framework.logging as logging_mod

    saved = getattr(sys, "_writeragent_watchdog_started", False)
    try:
        setattr(sys, "_writeragent_watchdog_started", False)
        with patch("plugin.framework.worker_pool.run_in_background", side_effect=RuntimeError("pool down")):
            with pytest.raises(RuntimeError, match="pool down"):
                logging_mod.start_watchdog_thread(None)
        assert getattr(sys, "_writeragent_watchdog_started") is False

        with patch("plugin.framework.worker_pool.run_in_background") as mock_run:
            logging_mod.start_watchdog_thread(None)
            assert mock_run.call_count == 1
            assert mock_run.call_args.kwargs.get("dedicated") is True
            assert getattr(sys, "_writeragent_watchdog_started") is True
    finally:
        setattr(sys, "_writeragent_watchdog_started", saved)


def test_log_record_factory_pyuno_str_failure_uses_placeholder() -> None:
    class pyuno:
        def __str__(self) -> str:
            raise RuntimeError("disposed")

    failing = pyuno()
    factory = logging.getLogRecordFactory()
    record1 = factory("test", logging.INFO, "file.py", 10, "value=%s", (failing,), None, None, None)
    assert record1.args == ("<pyuno>",)

    class pyuno:  # type: ignore[no-redef]
        def __str__(self) -> str:
            return "proxy-ok"

    ok = pyuno()
    record2 = factory("test", logging.INFO, "file.py", 10, "value=%s", (ok,), None, None, None)
    assert record2.args == ("proxy-ok",)

    plain_args = ("text",)
    record3 = factory("test", logging.INFO, "file.py", 10, "value=%s", plain_args, None, None, None)
    assert record3.args == ("text",)
    assert record3.args is plain_args

    record4 = factory("test", logging.INFO, "file.py", 10, "value=%s", args=(failing,), exc_info=None, func=None, sinfo=None)
    assert record4.args == ("<pyuno>",)



def test_debug_log_max_bytes_env(monkeypatch):
    import plugin.framework.logging as logging_mod

    monkeypatch.setenv("WRITERAGENT_DEBUG_LOG_MAX_MB", "200")
    assert logging_mod._debug_log_max_bytes() == 200 * 1024 * 1024
    monkeypatch.setenv("WRITERAGENT_DEBUG_LOG_MAX_MB", "junk")
    assert logging_mod._debug_log_max_bytes() == 5 * 1024 * 1024
    monkeypatch.delenv("WRITERAGENT_DEBUG_LOG_MAX_MB")
    assert logging_mod._debug_log_max_bytes() == 5 * 1024 * 1024
