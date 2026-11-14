
import pytest
from typing import Any
from plugin.framework.errors import (
    ToolExecutionError,
    WriterAgentException,
    format_error_for_display,
    format_error_payload,
    make_tool_error,
    resolve_exception_message,
)
from plugin.framework.tool import ToolBase
from unittest.mock import MagicMock
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

    def execute(self, ctx: Any = None, **kwargs: Any) -> Any:
        pass

class TestErrorHandling:

    def test_format_error_payload_writer_agent_exception(self):
        exc = WriterAgentException('Test message', code='TEST_CODE', details={'key': 'value'})
        payload = format_error_payload(exc)
        assert (payload['status']) == ('error')
        assert (payload['code']) == ('TEST_CODE')
        assert (payload['message']) == ('Test message')
        assert (payload['details']) == ({'key': 'value'})

    def test_format_error_payload_generic_exception(self):
        exc = ValueError('Test message')
        payload = format_error_payload(exc)
        assert (payload['status']) == ('error')
        assert (payload['code']) == ('INTERNAL_ERROR')
        assert (payload['message']) == ('Test message')
        assert (payload['details']) == ({'type': 'ValueError'})

    def test_tool_base_error_formatting(self):
        tool = DummyTool()
        result = tool._tool_error('Tool failed', code='CUSTOM_CODE', key='val')
        assert (result['status']) == ('error')
        assert (result['code']) == ('CUSTOM_CODE')
        assert (result['message']) == ('Tool failed')
        assert (result['details']) == ({'key': 'val'})

    def test_make_tool_error_top_level_fields(self):
        res = make_tool_error("m", code="C", top_level={"a": 1}, b=2)
        assert res["status"] == "error"
        assert res["code"] == "C"
        assert res["message"] == "m"
        assert res["a"] == 1
        assert res["details"] == {"b": 2}
        assert make_tool_error("m", code="C", top_level=None) == {"status": "error", "code": "C", "message": "m"}
        assert make_tool_error("m", code="C", top_level={}) == {"status": "error", "code": "C", "message": "m"}

    def test_make_tool_error_top_level_reserved_key_rejected(self):
        with pytest.raises(ValueError, match="top_level cannot override"):
            make_tool_error("m", top_level={"code": "X"})

    def test_tool_base_error_top_level_forwarded(self):
        assert DummyTool()._tool_error("m", top_level={"flag": True})["flag"] is True

    def test_format_error_for_display(self):
        exc = WriterAgentException('User error', code='ERR')
        display_str = format_error_for_display(exc)
        assert (display_str) == ('Error: User error')
        exc_generic = ValueError('System error')
        display_str_generic = format_error_for_display(exc_generic)
        assert (display_str_generic) == ('Error: System error')

    def test_format_error_for_display_payload_dict(self):
        display_str = format_error_for_display(
            {"status": "error", "code": "HTTP_ERROR", "message": "HTTP Error 500 from AI Provider"}
        )
        assert (display_str) == ("Error: HTTP Error 500 from AI Provider")

    def test_format_error_for_display_payload_dict_missing_message(self):
        display_str = format_error_for_display({"status": "error", "code": "HTTP_ERROR"})
        assert (display_str) == ("Error: HTTP_ERROR")
        display_empty = format_error_for_display({})
        assert (display_empty) == ("Error: {}")

    def test_librepy_format_error_message_advice(self):
        from plugin.framework.errors import format_error_message

        msg_generic_venv = format_error_message(RuntimeError("venv not found at /tmp/x"))
        assert ("Python venv not found") in (msg_generic_venv)

        msg_spill = format_error_message(RuntimeError("#SPILL!"))
        assert "Formula spill collision" in msg_spill
        assert "Formula spill collision" in format_error_message(RuntimeError("  #SPILL!  "))
        # Only the exact error string maps; a message that merely mentions it is passed through.
        assert format_error_message(RuntimeError("cell shows #SPILL! now")) == "cell shows #SPILL! now"

    def test_format_error_message_http_exception(self):
        import http.client
        from plugin.framework.errors import format_error_message

        err = http.client.RemoteDisconnected("Remote end closed connection")
        msg = format_error_message(err)
        # No HTTP status: the message must not look like "HTTP Error 0: " when
        # only "HTTP Error" is present.
        assert "Remote end closed connection" in msg
        assert "HTTP Error 0" not in msg

    def test_format_error_message_111_substring_is_not_connection_refused(self):
        import errno
        import urllib.error

        from plugin.framework.errors import format_error_message

        missed = urllib.error.URLError("failed to reach http://host:1111/v1")
        assert "Connection Refused" not in format_error_message(missed)
        refused = OSError(errno.ECONNREFUSED, "connect")
        assert "Connection Refused" in format_error_message(refused)

    def test_format_error_message_urlerror_timeout_is_request_timeout(self):
        import socket
        import urllib.error

        from plugin.framework.errors import format_error_message

        wrapped = urllib.error.URLError(socket.timeout("timed out"))
        assert "Request Timed Out" in format_error_message(wrapped)
        assert "Connection Error" not in format_error_message(wrapped)
        text = urllib.error.URLError("The read operation timed out")
        assert "Request Timed Out" in format_error_message(text)

    def test_format_error_message_filesystem_and_python_timeout_are_not_http(self):
        from plugin.framework.errors import format_error_message

        missing = format_error_message(FileNotFoundError("writeragent.json missing"))
        assert "Connection Error" not in missing
        assert "writeragent.json missing" in missing
        denied = format_error_message(PermissionError("settings unreadable"))
        assert "Connection Error" not in denied
        py_timeout = format_error_message(RuntimeError("Python execution timed out after 30 seconds"))
        assert "Request Timeout" not in py_timeout
        assert "Python" in py_timeout
        formula = format_error_message(RuntimeError("formula evaluation timed out"))
        assert "Request Timeout" not in formula
        assert "formula evaluation timed out" in formula

    def test_format_error_message_oserror_filesystem_vs_transport(self):
        import errno
        import socket
        from plugin.framework.errors import format_error_message

        # Filesystem errors fall through to the raw message without "Connection Error:"
        assert format_error_message(FileExistsError("cache exists")) == "cache exists"
        assert "Connection Error" not in format_error_message(BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"))
        assert "Connection Error" not in format_error_message(OSError(errno.ENOSPC, "No space left on device"))
        assert format_error_message(OSError("disk full")) == "disk full"
        assert format_error_message(OSError()) == "OSError"

        # Transport errors retain Connection Error: or specific sentences
        assert "Connection Error" in format_error_message(ConnectionResetError("Connection reset by peer"))
        assert "Connection Error" in format_error_message(OSError(errno.EHOSTUNREACH, "No route to host"))
        assert "Connection Error" in format_error_message(socket.gaierror(socket.EAI_NONAME, "Name or service not known"))
        refused_msg = format_error_message(OSError("connection refused"))
        assert "Connection Refused" in refused_msg

    def test_format_error_message_socket_timeout_vs_host_timeout(self):
        import socket
        from plugin.framework.errors import format_error_message

        # Socket / HTTP timeouts produce the Settings sentence
        assert "Request Timed Out" in format_error_message(TimeoutError("timed out"))
        assert "Settings" in format_error_message(TimeoutError("timed out"))
        assert "Request Timed Out" in format_error_message(socket.timeout("timed out"))
        assert "Settings" in format_error_message(socket.timeout("timed out"))

        # Non-socket host timeouts keep their original message and do not mention Settings
        host_messages = [
            "Main-thread execution of _workbook_session_id_impl timed out after 30.0s",
            "Harper LSP operation timed out",
            "marshal post timed out: AsyncCallback unavailable",
        ]
        for msg in host_messages:
            res = format_error_message(TimeoutError(msg))
            assert res == msg
            assert "Request Timed Out" not in res
            assert "Settings" not in res

        runtime_harper = format_error_message(RuntimeError("Harper LSP operation timed out"))
        assert runtime_harper == "Harper LSP operation timed out"
        assert "Request Timed Out" not in runtime_harper
        assert "Settings" not in runtime_harper

        os_timeout = format_error_message(OSError("connection timed out"))
        assert "Request Timed Out" in os_timeout
        assert "Settings" in os_timeout

    def test_http_status_message_matches_urllib_and_client_response(self):
        import urllib.error
        from email.message import Message
        from plugin.framework.client.errors import format_http_error_response
        from plugin.framework.errors import format_error_message, http_status_message

        for status in (401, 403, 404, 429):
            msg = http_status_message(status)
            assert msg is not None
            http_err = urllib.error.HTTPError("http://example.com", status, "Error", Message(), None)
            assert format_error_message(http_err) == msg
            assert format_http_error_response(status, "Error", "") == msg

        assert http_status_message(500) is None
        assert http_status_message(200) is None

    def test_writeragent_exception_subclasses_and_details_unification(self):
        from plugin.framework.errors import (
            AgentParsingError,
            ConfigError,
            ConfigValidationError,
            DocumentDisposedError,
            NetworkError,
            ToolExecutionError,
            UnoObjectError,
            WorkerPoolError,
        )
        from plugin.draw.shapes import DrawError
        from plugin.mcp.document_mutation_gate import BusyError

        subclasses = [
            (ConfigError("cfg err"), "CONFIG_ERROR"),
            (ConfigValidationError("val err"), "CONFIG_VALIDATION_ERROR"),
            (NetworkError("net err"), "NETWORK_ERROR"),
            (UnoObjectError("uno err"), "UNO_OBJECT_ERROR"),
            (WorkerPoolError("worker err"), "WORKER_ERROR"),
            (ToolExecutionError("tool err"), "TOOL_EXECUTION_ERROR"),
            (AgentParsingError("parse err"), "PARSE_ERROR"),
            (DrawError("draw err"), "DRAW_ERROR"),
            (BusyError("busy err"), "SERVER_BUSY"),
        ]

        for exc, expected_code in subclasses:
            assert (exc.code) == (expected_code)
            assert (exc.details) == ({})
            payload = format_error_payload(exc)
            assert (payload["code"]) == (expected_code)
            assert ("details") not in (payload)

        # Test overriding code and passing details
        custom_exc = ConfigError("bad config", code="CUSTOM_CFG", details={"key": "api_key"})
        assert (custom_exc.code) == ("CUSTOM_CFG")
        assert (custom_exc.details) == ({"key": "api_key"})
        payload = format_error_payload(custom_exc)
        assert (payload["code"]) == ("CUSTOM_CFG")
        assert (payload["details"]) == ({"key": "api_key"})

        # Test DocumentDisposedError with custom field object_type
        disp_exc = DocumentDisposedError("Object disposed", object_type="TextRange", details={"line": 42})
        assert (disp_exc.code) == ("DOCUMENT_DISPOSED")
        assert (disp_exc.object_type) == ("TextRange")
        assert (disp_exc.details) == ({"line": 42})


    def test_long_runtime_message_does_not_raise_precontract(self):
        """Provider and UNO text longer than the msgid bound must still construct.

        Passing provider text through ``i18n._()`` rejects strings longer than
        ``DEAL_MAX_MSGID``; long runtime messages must still construct.
        """
        from plugin.framework.deal_shim import DEAL_MAX_MSGID
        from plugin.framework.errors import NetworkError, make_tool_error

        text = "provider body " + ("x" * (DEAL_MAX_MSGID + 50))
        err = NetworkError(text)
        assert err.message == text
        assert isinstance(err, WriterAgentException)
        payload = make_tool_error(text, code="TOOL_EXECUTION_ERROR")
        assert payload["status"] == "error"
        assert payload["message"] == text
        assert payload["code"] == "TOOL_EXECUTION_ERROR"


def test_best_effort_logs_and_swallows_everything(caplog):
    import logging

    from plugin.framework.errors import DocumentDisposedError, best_effort

    class DisposedException(Exception):
        pass

    assert best_effort(lambda: 7, "never logged") == 7
    with caplog.at_level(logging.DEBUG, logger="writeragent.errors"):
        for exc in (DisposedException("closed"), DocumentDisposedError("closed"), ValueError("bad")):
            def boom(exc: Exception = exc) -> int:
                raise exc
            assert best_effort(boom, "cleanup step failed") is None
            assert best_effort(boom, "cleanup step failed", 3) == 3
    records = [r for r in caplog.records if r.getMessage() == "cleanup step failed"]
    assert len(records) == 6 and all(r.levelno == logging.DEBUG and r.exc_info for r in records)


class TestSecurityFix:

    def test_nested_structures_no_crash(self):
        depth = 5000
        nested_list_str = (('[' * depth) + (']' * depth))
        try:
            result = safe_python_literal_eval(nested_list_str, default='fallback')
            assert (isinstance(result, list) or (result == 'fallback'))
        except Exception as e:
            pytest.fail(f'safe_python_literal_eval crashed with {type(e).__name__}: {e}')

    def test_large_input_no_crash(self):
        large_input = (('[' + ('1,' * 1000000)) + '1]')
        try:
            result = safe_python_literal_eval(large_input, default='fallback')
            assert (isinstance(result, list) or (result == 'fallback'))
        except Exception as e:
            pytest.fail(f'safe_python_literal_eval crashed with {type(e).__name__}: {e}')

    def test_common_literals(self):
        assert (safe_python_literal_eval('True')) is True
        assert (safe_python_literal_eval('true')) is True
        assert (safe_python_literal_eval('False')) is False
        assert (safe_python_literal_eval('false')) is False
        assert (safe_python_literal_eval('None')) is None
        assert (safe_python_literal_eval('none')) is None
        assert (safe_python_literal_eval('null')) is None
        assert (safe_python_literal_eval('NULL')) is None
        assert (safe_python_literal_eval('123')) == (123)
        assert (safe_python_literal_eval('"hello"')) == ('hello')
        assert (safe_python_literal_eval("'hello'")) == ('hello')

    def test_json_structures(self):
        assert (safe_python_literal_eval('[1, 2, 3]')) == ([1, 2, 3])
        assert (safe_python_literal_eval('{"a": 1}')) == ({'a': 1})

    def test_single_quoted_strings_restricted(self):
        assert (safe_python_literal_eval("'safe'")) == ('safe')
        assert (safe_python_literal_eval("'it\\'s unsafe'", default='fallback')) == ('fallback')

    def test_non_json_python_literals_fallback(self):
        assert (safe_python_literal_eval('(1, 2)', default='(1, 2)')) == ('(1, 2)')
        assert (safe_python_literal_eval("{'a': 1}", default='fallback')) == ('fallback')

    def test_smolagents_deserializer(self):
        assert (safe_python_literal_eval('{"type": "string"}')) == ({'type': 'string'})


class TestSuppressDisposed:

    def test_is_disposal_bare_runtime_not_disposal(self):
        from plugin.framework.errors import (
            DocumentDisposedError,
            is_disposal,
        )

        class CustomDisposedException(Exception):
            pass

        class CustomRuntimeException(Exception):
            pass

        # Bare RuntimeException is a real UNO error, not dispose.
        assert not is_disposal(CustomRuntimeException(""))
        assert is_disposal(CustomDisposedException("Disposed"))
        assert is_disposal(DocumentDisposedError("gone"))
        assert not is_disposal(ValueError("Bad value"))

    def test_disposal_predicates_truth_table(self):
        from plugin.framework.errors import (
            DocumentDisposedError,
            document_is_disposed,
            is_disposal,
        )

        class CustomDisposedException(Exception):
            pass

        class CustomRuntimeException(Exception):
            pass

        class NoCallableDoc:
            getImplementationName = "not_callable"

        class LiveDoc:
            def getImplementationName(self):
                return "SwXTextDocument"

        class DisposedProbeDoc:
            def getImplementationName(self):
                raise CustomDisposedException("gone")

        class RuntimeProbeDoc:
            def getImplementationName(self):
                raise CustomRuntimeException("probe failed")

        docs = [
            ("none", None, True),
            ("no_callable", NoCallableDoc(), False),
            ("live", LiveDoc(), False),
            ("disposed_probe", DisposedProbeDoc(), True),
            ("runtime_probe", RuntimeProbeDoc(), False),
        ]

        # Verify document_is_disposed directly on each document
        for _doc_name, doc_obj, expected_doc_disposed in docs:
            assert document_is_disposed(doc_obj) is expected_doc_disposed

        # Exception kinds: (exc, expected_disposal)
        exceptions: list[tuple[str, BaseException, bool]] = [
            ("DocumentDisposedError", DocumentDisposedError("gone"), True),
            ("DisposedException", CustomDisposedException("disposed"), True),
            ("RuntimeException", CustomRuntimeException("runtime"), False),
            ("ValueError", ValueError("bad value"), False),
        ]

        try:
            import uno  # noqa: F401
            from com.sun.star.lang import IllegalArgumentException

            exceptions.append(("IllegalArgumentException", IllegalArgumentException("arg"), False))
        except (ImportError, AttributeError):
            pass

        for exc_name, exc, exp_disposal in exceptions:
            assert is_disposal(exc) is exp_disposal, f"{exc_name} is_disposal mismatch"

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

        assert (executed)
        if _suppress_disposed_debug_logs_present():
            mock_logger.debug.assert_called_once()
            assert (mock_logger.debug.call_args[0][1]) == ("test_disposed_action")
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

        assert (executed)
        mock_logger.exception.assert_called_once()
        assert (mock_logger.exception.call_args[0][1]) == ("test_unexpected_action")
        mock_logger.debug.assert_not_called()

    def test_suppress_disposed_unexpected_error_raised(self):
        from plugin.framework.errors import suppress_disposed

        mock_logger = MagicMock()
        with pytest.raises(ValueError):
            with suppress_disposed("test_raise_action", logger=mock_logger, suppress_all=False):
                raise ValueError("Must be raised")

        mock_logger.exception.assert_called_once()
        assert (mock_logger.exception.call_args[0][1]) == ("test_raise_action")

    def test_suppress_disposed_as_decorator(self):
        from plugin.framework.errors import (
            DocumentDisposedError,
            suppress_disposed,
        )

        mock_logger = MagicMock()

        @suppress_disposed("decorated_function", logger=mock_logger)
        def faulty_fn():
            raise DocumentDisposedError("Peer disposed")

        result = faulty_fn()
        assert (result) is None
        if _suppress_disposed_debug_logs_present():
            mock_logger.debug.assert_called_once()
        else:
            mock_logger.debug.assert_not_called()

    def test_suppress_disposed_swallows_runtime_exception_not_illegal_argument(self):
        from plugin.framework.errors import suppress_disposed

        class RuntimeException(Exception):
            pass

        class IllegalArgumentException(Exception):
            pass

        executed = False
        with suppress_disposed("test_runtime", suppress_all=False):
            executed = True
            raise RuntimeException("bridge teardown")
        assert executed

        with pytest.raises(IllegalArgumentException):
            with suppress_disposed("test_illegal_arg", suppress_all=False):
                raise IllegalArgumentException("bad argument")


def test_illegal_argument_is_not_document_disposal():
    """Real pyuno: IllegalArgumentException subclasses RuntimeException.

    ``IllegalArgumentException`` subclasses ``RuntimeException``. Treating every
    ``RuntimeException`` as disposal mislabels real argument errors; ``uno_call``
    must wrap a bare ``RuntimeException`` as ``UnoObjectError``.
    """
    uno = pytest.importorskip("uno")
    del uno
    try:
        from com.sun.star.lang import DisposedException, IllegalArgumentException, IndexOutOfBoundsException
        from com.sun.star.uno import RuntimeException
    except ImportError:
        pytest.skip("pyuno type library is not available")

    from plugin.framework.errors import (
        UnoObjectError,
        is_disposal,
        uno_call,
    )

    bad_arg = IllegalArgumentException("bad arg", None, 0)
    assert not is_disposal(bad_arg)

    def _raise_arg():
        raise bad_arg

    with pytest.raises(UnoObjectError):
        uno_call(_raise_arg, "probe")

    assert not is_disposal(IndexOutOfBoundsException("oob", None))
    assert is_disposal(DisposedException("gone", None))
    runtime = RuntimeException("bridge", None)
    assert not is_disposal(runtime)

    def _raise_runtime():
        raise runtime

    with pytest.raises(UnoObjectError):
        uno_call(_raise_runtime, "probe")


def test_uno_call_reraises_writer_agent_exception():
    from plugin.framework.errors import ConfigError, uno_call

    def _fail():
        raise ConfigError("Permission denied")

    with pytest.raises(ConfigError) as exc_info:
        uno_call(_fail, "probe")
    assert exc_info.value.code == "CONFIG_ERROR"


def test_uno_read_wraps_real_disposal_and_defaults_otherwise():
    from plugin.framework.errors import DocumentDisposedError, uno_read

    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    def boom(exc):
        def fn():
            raise exc

        return fn

    assert uno_read(lambda: "v", "") == "v"
    assert uno_read(boom(ValueError("bad")), "") == ""
    # A bare RuntimeException is a live-document failure, not disposal (same as uno_read).
    assert uno_read(boom(RuntimeException("bridge")), None) is None
    # Real disposal leaves wrapped or unchanged, as reraise_if_disposed does.
    disposed_exc = DisposedException("dead")
    with pytest.raises(DocumentDisposedError) as exc_info:
        uno_read(boom(disposed_exc), None)
    assert exc_info.value.__cause__ is disposed_exc

    doc_disposed_exc = DocumentDisposedError("closed")
    with pytest.raises(DocumentDisposedError) as exc_info:
        uno_read(boom(doc_disposed_exc), None)
    assert exc_info.value is doc_disposed_exc
    with pytest.raises(DocumentDisposedError) as exc_info:
        uno_read(boom(DisposedException("dead")), None, object_type="TextRange")
    assert exc_info.value.object_type == "TextRange"
    with pytest.raises(KeyboardInterrupt):
        uno_read(boom(KeyboardInterrupt()), None)


def test_uno_write_false_on_runtime_and_reraises_original_disposal(caplog):
    import logging

    from plugin.framework.errors import DocumentDisposedError, uno_write

    class DisposedException(Exception):
        pass

    assert uno_write(lambda: None) is True
    with caplog.at_level(logging.DEBUG, logger="writeragent.errors"):
        assert uno_write(lambda: (_ for _ in ()).throw(RuntimeError("bridge"))) is False
    assert not caplog.records

    disposed_exc = DisposedException("dead")
    with pytest.raises(DisposedException) as exc_info:
        uno_write(lambda: (_ for _ in ()).throw(disposed_exc))
    assert exc_info.value is disposed_exc

    doc_disposed = DocumentDisposedError("closed")
    with pytest.raises(DocumentDisposedError) as exc_info:
        uno_write(lambda: (_ for _ in ()).throw(doc_disposed))
    assert exc_info.value is doc_disposed

    with pytest.raises(KeyboardInterrupt):
        uno_write(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))


def test_format_error_message_urlerror_ssl_is_tls():
    import ssl
    import urllib.error
    from plugin.framework.errors import format_error_message

    cert_err = ssl.SSLCertVerificationError("self-signed certificate")
    url_err = urllib.error.URLError(cert_err)
    msg = format_error_message(url_err)
    assert "TLS/SSL Error" in msg
    assert "self-signed certificate" in msg
    assert "Connection Error" not in msg

    direct_ssl = ssl.SSLError("cert error")
    msg_direct = format_error_message(direct_ssl)
    assert "TLS/SSL Error" in msg_direct
    assert "cert error" in msg_direct


def test_format_error_message_uses_uno_message_attribute():
    from plugin.framework.errors import format_error_message

    class CustomUnoException(Exception):
        def __init__(self, message: str):
            super().__init__("")
            self.Message = message

        def __str__(self) -> str:
            return ""

    exc = CustomUnoException("sheet is protected")
    msg = format_error_message(exc)
    assert "sheet is protected" in msg


def test_uno_call_uses_uno_message():
    from plugin.framework.errors import UnoObjectError, uno_call

    class CustomUnoException(Exception):
        def __init__(self, message: str):
            super().__init__("")
            self.Message = message

        def __str__(self) -> str:
            return ""

    class RuntimeException(Exception):
        def __str__(self) -> str:
            return ""

    exc = CustomUnoException("sheet is protected")

    def _raise_exc():
        raise exc

    with pytest.raises(UnoObjectError) as exc_info:
        uno_call(_raise_exc, "insert")
    assert "sheet is protected" in str(exc_info.value)

    runtime_exc = RuntimeException()

    def _raise_runtime():
        raise runtime_exc

    with pytest.raises(UnoObjectError):
        uno_call(_raise_runtime, "insert")


def test_resolve_exception_message_total():
    from plugin.framework.errors import format_error_message

    class StrExplodes(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str exploded")

    class MsgExplodes(Exception):
        @property
        def Message(self) -> str:
            raise RuntimeError("Message exploded")

    class BlankStrWithMessage(Exception):
        def __init__(self, msg: str):
            super().__init__("")
            self.Message = msg

        def __str__(self) -> str:
            return ""

    assert resolve_exception_message(StrExplodes()) == "StrExplodes"
    assert format_error_message(StrExplodes()) == "StrExplodes"

    assert resolve_exception_message(MsgExplodes()) == "MsgExplodes"
    assert format_error_message(MsgExplodes()) == "MsgExplodes"

    assert resolve_exception_message(BlankStrWithMessage("uno message")) == "uno message"
    assert format_error_message(BlankStrWithMessage("uno message")) == "uno message"
    assert format_error_payload(ToolExecutionError(BlankStrWithMessage("uno message")))["message"] == "uno message"

    class UnoEmptyMessage(Exception):
        __pyunostruct__ = "com.sun.star.uno.RuntimeException"
        Message = ""

        def __str__(self) -> str:
            raise AssertionError("str() called on hostile UNO exception with empty message")

    assert resolve_exception_message(UnoEmptyMessage()) == "UnoEmptyMessage"
    assert format_error_message(UnoEmptyMessage()) == "UnoEmptyMessage"
    assert format_error_payload(UnoEmptyMessage())["message"] == "UnoEmptyMessage"

    kb = KeyboardInterrupt()
    assert resolve_exception_message(kb) == "KeyboardInterrupt"
    assert format_error_message(kb) == "KeyboardInterrupt"


def test_make_tool_error_accepts_non_str_and_non_ascii_code():
    from plugin.framework.errors import make_tool_error

    res = make_tool_error(ValueError("value issue"))
    assert res["status"] == "error"
    assert res["message"] == "value issue"
    assert res["code"] == "TOOL_EXECUTION_ERROR"

    res_code = make_tool_error("hello", code="代码")
    assert res_code["status"] == "error"
    assert res_code["code"] == "代码"

    res_empty_code = make_tool_error("hello", code="")
    assert res_empty_code["code"] == "TOOL_EXECUTION_ERROR"


def test_is_document_disposed_handles_exploding_attributes():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.framework.errors import document_is_disposed

    class Dead(Exception):
        pass

    class DeadProxy:
        def __getattribute__(self, name: str) -> Any:
            raise Dead("proxy disposed")

    class ExplodingProxy:
        def __getattribute__(self, name: str) -> Any:
            raise RuntimeError("unexpected error")

    class DeadWithDisposedException(Exception):
        pass

    DeadWithDisposedException.__name__ = "com.sun.star.lang.DisposedException"

    class DisposedProxy:
        def __getattribute__(self, name: str) -> Any:
            raise DeadWithDisposedException("disposed")

    class DocumentDisposedProxy:
        def __getattribute__(self, name: str) -> Any:
            raise DocumentDisposedError("doc disposed")

    assert document_is_disposed(None) is True
    assert document_is_disposed(DeadProxy()) is False
    assert document_is_disposed(ExplodingProxy()) is False
    assert document_is_disposed(DisposedProxy()) is True
    assert document_is_disposed(DocumentDisposedProxy()) is True


def test_writer_agent_exception_unified_message():
    from plugin.framework.errors import ToolExecutionError, WriterAgentException

    class BlankWithUnoMessage(Exception):
        def __init__(self, msg: str):
            super().__init__("")
            self.Message = msg

        def __str__(self) -> str:
            return ""

    exc_uno = BlankWithUnoMessage("UNO detail")
    tool_err = ToolExecutionError(exc_uno)
    assert str(tool_err) == "UNO detail"
    assert tool_err.args[0] == "UNO detail"
    assert tool_err.message == "UNO detail"

    plain = WriterAgentException("plain text")
    assert str(plain) == "plain text"
    assert plain.args[0] == "plain text"
    assert plain.message == "plain text"


def test_reraise_if_disposed_preserves_instance():
    from plugin.framework.errors import DocumentDisposedError, reraise_if_disposed, uno_read

    class LocalDisposedException(Exception):
        pass

    LocalDisposedException.__name__ = "com.sun.star.lang.DisposedException"

    class RuntimeException(Exception):
        pass

    orig = DocumentDisposedError("keep this", object_type="TextRange", details={"line": 7})
    with pytest.raises(DocumentDisposedError) as exc_info:
        reraise_if_disposed(orig)
    assert exc_info.value is orig
    assert exc_info.value.object_type == "TextRange"
    assert exc_info.value.details == {"line": 7}

    with pytest.raises(DocumentDisposedError) as exc_info2:
        def _raise_orig():
            raise orig

        uno_read(_raise_orig, "default")
    assert exc_info2.value is orig

    local_exc = LocalDisposedException("object closed")
    with pytest.raises(DocumentDisposedError) as exc_info3:
        reraise_if_disposed(local_exc, "wrapper message", object_type="sheet")
    assert exc_info3.value.__cause__ is local_exc
    assert exc_info3.value.object_type == "sheet"
    assert str(exc_info3.value) == "wrapper message"

    reraise_if_disposed(RuntimeException("live error"))
    assert uno_read(lambda: 1 / 0, "fallback") == "fallback"


def test_format_error_for_display_connection_refused():
    from plugin.framework.errors import format_error_for_display

    exc = ConnectionRefusedError(111, "Connection refused")
    display = format_error_for_display(exc)
    assert "Connection Refused" in display
    assert "local AI server" in display


from tests.framework.suggested_call_access import (
    assert_valid_suggested_call,
    validate_suggested_call,
)


def test_suggest_call_shape():
    from plugin.framework.errors import suggest_call

    suggestion = suggest_call("apply_style", target="search", style="Heading 2")
    assert suggestion == {
        "tool": "apply_style",
        "arguments": {"target": "search", "style": "Heading 2"},
    }

    no_args = suggest_call("get_document_content")
    assert no_args == {"tool": "get_document_content", "arguments": {}}


def test_format_error_payload_suggested_call():
    from plugin.framework.errors import WriterAgentException, format_error_payload, suggest_call

    call = suggest_call("apply_style", style="Heading 1")

    # With WriterAgentException and details
    exc = WriterAgentException("Style application failed", code="STYLE_FAILED", details={"pos": 10})
    payload = format_error_payload(exc, suggested_call=call)
    assert payload["status"] == "error"
    assert payload["code"] == "STYLE_FAILED"
    assert payload["message"] == "Style application failed"
    assert payload["suggested_call"] == call
    assert payload["details"] == {"pos": 10}

    # Without suggested_call, key is omitted
    payload_plain = format_error_payload(exc)
    assert "suggested_call" not in payload_plain

    # With generic unexpected exception
    raw_exc = RuntimeError("Unexpected disk error")
    payload_generic = format_error_payload(raw_exc, suggested_call=call)
    assert payload_generic["status"] == "error"
    assert payload_generic["code"] == "INTERNAL_ERROR"
    assert payload_generic["message"] == "Unexpected disk error"
    assert payload_generic["suggested_call"] == call
    assert payload_generic["details"] == {"type": "RuntimeError"}


def test_make_tool_error_suggested_call():
    from plugin.framework.errors import make_tool_error, suggest_call

    call = suggest_call("apply_style", target="search", old_content="Intro", style="Heading 1")

    # With suggested_call and extra kwargs (kwargs must go under details)
    res = make_tool_error(
        "Style missing",
        code="STYLE_NOT_FOUND",
        suggested_call=call,
        context_flag=True,
    )
    assert res["status"] == "error"
    assert res["code"] == "STYLE_NOT_FOUND"
    assert res["message"] == "Style missing"
    assert res["suggested_call"] == call
    assert res["details"] == {"context_flag": True}

    # With suggested_call and no extra kwargs (details must be omitted)
    res_no_details = make_tool_error(
        "Failed edit",
        code="EDIT_FAILED",
        suggested_call=call,
    )
    assert res_no_details["status"] == "error"
    assert res_no_details["code"] == "EDIT_FAILED"
    assert res_no_details["suggested_call"] == call
    assert "details" not in res_no_details

    # Without suggested_call
    res_plain = make_tool_error("Failed edit", code="EDIT_FAILED")
    assert "suggested_call" not in res_plain


def test_tool_base_error_formatting_with_suggested_call():
    from plugin.framework.errors import suggest_call

    tool = DummyTool()
    call = suggest_call("apply_style", style="Heading 1")
    result = tool._tool_error(
        "Style lookup failed",
        code="STYLE_NOT_FOUND",
        suggested_call=call,
        candidate_count=0,
    )
    assert result["status"] == "error"
    assert result["code"] == "STYLE_NOT_FOUND"
    assert result["message"] == "Style lookup failed"
    assert result["suggested_call"] == call
    assert result["details"] == {"candidate_count": 0}


def test_validate_suggested_call_against_apply_style():
    from plugin.framework.errors import suggest_call

    # Valid apply_style suggestion
    valid_call = suggest_call(
        "apply_style",
        target="search",
        old_content="Quarterly results",
        style="Heading 2",
        case_sensitive=True,
    )
    assert_valid_suggested_call(valid_call)

    # Missing required parameter ('style')
    missing_style = suggest_call(
        "apply_style",
        target="search",
        old_content="Quarterly results",
    )
    ok, err = validate_suggested_call(missing_style)
    assert not ok
    assert err is not None and "Missing required parameter: style" in err

    # Unknown parameter (e.g. MCP's document_url or internal flag)
    unknown_param = suggest_call(
        "apply_style",
        style="Heading 2",
        document_url="vnd.sun.star.tdoc:/1",
    )
    ok, err = validate_suggested_call(unknown_param)
    assert not ok
    assert err is not None and "Unknown parameter: document_url" in err

    # Invalid parameter type (style must be string, not integer)
    bad_type = suggest_call(
        "apply_style",
        style=42,
    )
    ok, err = validate_suggested_call(bad_type)
    assert not ok
    assert err is not None and "Invalid type for style" in err

    # Invalid enum value (target must be in INSERT_TARGETS)
    bad_enum = suggest_call(
        "apply_style",
        style="Heading 2",
        target="unsupported_target",
    )
    ok, err = validate_suggested_call(bad_enum)
    assert not ok
    assert err is not None and "Invalid value for target" in err


def test_is_document_disposed():
    from plugin.framework.errors import document_is_disposed

    assert document_is_disposed(None) is True

    class DisposedExceptionMock(Exception):
        pass

    # Ensure the type name is EXACTLY 'DisposedException' for the check
    DisposedExceptionMock.__name__ = "DisposedException"

    disposed_exc = DisposedExceptionMock("Document disposed")

    class MockDisposedDoc:
        def getImplementationName(self):
            raise disposed_exc

    class MockValidDoc:
        def getImplementationName(self):
            return "SwXTextDocument"

    assert document_is_disposed(MockDisposedDoc()) is True
    assert document_is_disposed(MockValidDoc()) is False


def test_similar_names_caps_and_cutoff():
    from plugin.framework.errors import similar_names

    names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"]
    assert similar_names("", names) == []
    close = similar_names("Alfa", names)
    assert "Alpha" in close
    assert len(close) <= 5
    assert "Zeta" not in similar_names("Alfa", ["Zeta"])
    assert similar_names("totally_unrelated_xyz", names) == []
