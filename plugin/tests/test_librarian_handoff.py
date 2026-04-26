from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
import sys


class _UnoBase:
    pass


class _XActionListener:
    pass


class _XTextListener:
    pass


class _XWindowListener:
    pass


class _XItemListener:
    pass


class _XEventListener:
    pass


sys.modules.setdefault("uno", MagicMock())
unohelper_module = sys.modules.setdefault("unohelper", MagicMock())
unohelper_module.Base = _UnoBase

com_module = sys.modules.setdefault("com", ModuleType("com"))
sun_module = sys.modules.setdefault("com.sun", ModuleType("com.sun"))
star_module = sys.modules.setdefault("com.sun.star", ModuleType("com.sun.star"))
awt_module = sys.modules.setdefault("com.sun.star.awt", ModuleType("com.sun.star.awt"))
lang_module = sys.modules.setdefault("com.sun.star.lang", ModuleType("com.sun.star.lang"))
setattr(com_module, "sun", sun_module)
setattr(sun_module, "star", star_module)
setattr(star_module, "awt", awt_module)
setattr(star_module, "lang", lang_module)
setattr(awt_module, "XActionListener", _XActionListener)
setattr(awt_module, "XTextListener", _XTextListener)
setattr(awt_module, "XWindowListener", _XWindowListener)
setattr(awt_module, "XItemListener", _XItemListener)
setattr(lang_module, "XEventListener", _XEventListener)


from plugin.modules.chatbot.panel import SendButtonListener, format_grammar_status


def _make_listener(*, in_librarian_mode: bool) -> SimpleNamespace:
    query_control = MagicMock()
    query_control.getModel.return_value = True

    return SimpleNamespace(
        ctx=MagicMock(),
        ensure_path_fn=None,
        initial_doc_type=None,
        audio_wav_path=None,
        web_research_checkbox=None,
        direct_image_checkbox=None,
        query_control=query_control,
        _in_librarian_mode=in_librarian_mode,
        _terminal_status="Ready",
        _set_status=MagicMock(),
        _get_document_model=MagicMock(return_value=object()),
        _append_response=MagicMock(),
        _get_doc_type_str=MagicMock(return_value="Writer"),
        _run_librarian=MagicMock(),
        _do_send_chat_with_tools=MagicMock(),
        _do_send_direct_image=MagicMock(),
        _do_send_via_agent_backend=MagicMock(),
    )


def test_format_grammar_status_complete() -> None:
    text = format_grammar_status(
        {
            "phase": "complete",
            "preview": "This are bad",
            "length": 42,
            "result": "1 issue",
            "elapsed_ms": 812,
        }
    )

    assert text == "Grammar: done 'This are bad' len 42: 1 issue, 812ms"


def test_do_send_enters_librarian_when_user_memory_missing():
    listener = _make_listener(in_librarian_mode=False)

    with patch("plugin.modules.chatbot.panel.update_activity_state"), patch(
        "plugin.framework.dialogs.get_control_text", return_value="Hello"
    ), patch("plugin.framework.dialogs.set_control_text"), patch(
        "plugin.framework.config.get_config", return_value=None
    ), patch(
        "plugin.modules.agent_backend.registry.normalize_backend_id", return_value="builtin"
    ), patch("plugin.modules.chatbot.memory.MemoryStore") as mock_store:
        mock_store.return_value.read.return_value = ""
        SendButtonListener._do_send(listener)

    assert listener._in_librarian_mode is True
    listener._run_librarian.assert_called_once()
    listener._do_send_chat_with_tools.assert_not_called()


def test_do_send_stays_in_librarian_mode_without_rechecking_memory():
    listener = _make_listener(in_librarian_mode=True)

    with patch("plugin.modules.chatbot.panel.update_activity_state"), patch(
        "plugin.framework.dialogs.get_control_text", return_value="Hello again"
    ), patch("plugin.framework.dialogs.set_control_text"), patch(
        "plugin.framework.config.get_config", return_value=None
    ), patch(
        "plugin.modules.agent_backend.registry.normalize_backend_id", return_value="builtin"
    ), patch("plugin.modules.chatbot.memory.MemoryStore") as mock_store:
        SendButtonListener._do_send(listener)

    assert listener._in_librarian_mode is True
    mock_store.assert_not_called()
    listener._run_librarian.assert_called_once()
    listener._do_send_chat_with_tools.assert_not_called()


def test_do_send_uses_document_chat_after_librarian_flag_clears():
    listener = _make_listener(in_librarian_mode=False)

    with patch("plugin.modules.chatbot.panel.update_activity_state"), patch(
        "plugin.framework.dialogs.get_control_text", return_value="Work on the document"
    ), patch("plugin.framework.dialogs.set_control_text"), patch(
        "plugin.framework.config.get_config", return_value=None
    ), patch(
        "plugin.modules.agent_backend.registry.normalize_backend_id", return_value="builtin"
    ), patch("plugin.modules.chatbot.memory.MemoryStore") as mock_store:
        mock_store.return_value.read.return_value = '{"name": "Keith"}'
        SendButtonListener._do_send(listener)

    assert listener._in_librarian_mode is False
    listener._run_librarian.assert_not_called()
    listener._do_send_chat_with_tools.assert_called_once()
