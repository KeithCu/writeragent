"""Import-graph ownership tests for the sidebar factory (no UNO import)."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FACTORY = _REPO_ROOT / "plugin" / "chatbot" / "panel_factory.py"
_DIALOG_VIEWS = _REPO_ROOT / "plugin" / "chatbot" / "dialog_views.py"
_SEND_HANDLERS = _REPO_ROOT / "plugin" / "chatbot" / "send_handlers.py"
_TOOL_LOOP = _REPO_ROOT / "plugin" / "chatbot" / "tool_loop.py"
_TOOL_LOOP_ACTIONS = _REPO_ROOT / "plugin" / "chatbot" / "tool_loop_actions.py"


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            for alias in node.names:
                names.add(alias.name)
                if module:
                    names.add("%s.%s" % (module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def _calls_name(path: Path, func_name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == func_name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == func_name:
            return True
    return False


def test_panel_factory_does_not_import_or_call_chat_context_builder():
    """Factory wires XDL/controls. Document context is ChatSession / send / tool_loop."""
    imported = _imported_names(_FACTORY)
    assert "get_document_context_for_chat" not in imported
    assert "plugin.doc.document_helpers.get_document_context_for_chat" not in imported
    assert not _calls_name(_FACTORY, "get_document_context_for_chat")


def test_panel_factory_mode_switch_delegates_context_refresh_to_session():
    src = _FACTORY.read_text(encoding="utf-8")
    assert "session.refresh_document_context(model, self.ctx)" in src
    assert "_refresh_doc_session_context" not in src


def test_dialog_views_does_not_import_tool_loop():
    imported = _imported_names(_DIALOG_VIEWS)
    assert "plugin.chatbot.tool_loop" not in imported
    assert "tool_loop" not in imported


def test_send_and_tool_loop_do_not_import_panel_factory():
    for path in (_SEND_HANDLERS, _TOOL_LOOP, _TOOL_LOOP_ACTIONS):
        imported = _imported_names(path)
        assert "plugin.chatbot.panel_factory" not in imported
        assert "panel_factory" not in imported


def test_send_and_tool_loop_refresh_via_session_not_builder():
    """Send / mid-loop refresh go through ChatSession.refresh_document_context."""
    for path in (_SEND_HANDLERS, _TOOL_LOOP, _TOOL_LOOP_ACTIONS):
        imported = _imported_names(path)
        assert "get_document_context_for_chat" not in imported
        assert "plugin.doc.document_helpers.get_document_context_for_chat" not in imported
        src = path.read_text(encoding="utf-8")
        assert "refresh_document_context" in src


class _MockDisposedException(Exception):
    """Name must include DisposedException so is_disposed_exception matches."""


def _thin_panel_element():
    from plugin.chatbot.panel_factory import ChatPanelElement

    el = object.__new__(ChatPanelElement)
    el.send_listener = None
    el.toolpanel = None
    el.m_panelRootWindow = None
    el.rich_text_widget = None
    return el


def test_setup_sessions_passes_panel_ctx_into_seeded_prompt():
    """The seed must match refresh: vision, peer, and profile injection need ctx."""
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = object()
    model = MagicMock()
    model.getURL.return_value = ""

    with (
        patch(
            "plugin.chatbot.panel_factory.get_chat_system_prompt_for_document",
            return_value="SEEDED",
        ) as prompt,
        patch("plugin.chatbot.panel_factory.get_document_property", return_value=None),
        patch("plugin.chatbot.panel_factory.set_document_property"),
        patch("plugin.chatbot.panel.ChatSession", return_value=MagicMock()),
    ):
        el._setup_sessions(model, "extra")

    prompt.assert_called_once_with(model, "extra", ctx=el.ctx)


def _save_as_sessions(tmp_path, props: dict, url: str):
    """Run _setup_sessions against a temp history db. Returns (element, history getter)."""
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.history_db import get_chat_history

    el = _thin_panel_element()
    el.ctx = object()
    model = MagicMock()
    model.getURL.return_value = url
    db_path = str(tmp_path / "writeragent_history.db")

    def _get(_model, name, default=None):
        return props.get(name, default)

    def _set(_model, name, value):
        props[name] = value

    def _history(session_id, path=None):
        return get_chat_history(session_id, db_path)

    with (
        patch("plugin.chatbot.panel_factory.get_chat_system_prompt_for_document", return_value="SEEDED"),
        patch("plugin.chatbot.panel_factory.get_document_property", side_effect=_get),
        patch("plugin.chatbot.panel_factory.set_document_property", side_effect=_set),
        patch("plugin.chatbot.history_db.get_chat_history", side_effect=_history),
        patch("plugin.chatbot.panel.ChatSession", return_value=MagicMock()),
    ):
        el._setup_sessions(model, "")
    return get_chat_history


def test_setup_sessions_save_as_same_id_does_not_clear_history(tmp_path):
    """A regenerated id that already matches the stored id must not wipe that chat."""
    import hashlib

    new_url = "file:///tmp/copy.odt"
    new_id = hashlib.sha256(new_url.encode("utf-8")).hexdigest()
    props = {
        "WriterAgentSessionID": new_id,
        "WriterAgentSessionURL": "file:///tmp/original.odt",
    }
    db_path = str(tmp_path / "writeragent_history.db")
    from plugin.chatbot.history_db import get_chat_history

    get_chat_history(new_id, db_path).add_message("user", "only copy", [{"id": "call-1"}])
    history = _save_as_sessions(tmp_path, props, new_url)
    assert props["WriterAgentSessionID"] == new_id
    assert props["WriterAgentSessionURL"] == new_url
    msgs = history(new_id, db_path).get_messages()
    assert msgs == [{"role": "user", "content": "only copy", "tool_calls": [{"id": "call-1"}]}]


def test_setup_sessions_missing_session_url_forks_url_hash_id(tmp_path):
    """A copied file with a 64-hex id and no SessionURL must not keep the original chat id."""
    import hashlib

    from plugin.chatbot.history_db import get_chat_history

    old_url = "file:///tmp/original.odt"
    new_url = "file:///tmp/copy.odt"
    old_id = hashlib.sha256(old_url.encode("utf-8")).hexdigest()
    new_id = hashlib.sha256(new_url.encode("utf-8")).hexdigest()
    props = {"WriterAgentSessionID": old_id}
    db_path = str(tmp_path / "writeragent_history.db")
    get_chat_history(old_id, db_path).add_message("user", "from original")
    get_chat_history(old_id + "_web", db_path).add_message("user", "from web original")
    history = _save_as_sessions(tmp_path, props, new_url)
    assert props["WriterAgentSessionID"] == new_id
    assert props["WriterAgentSessionURL"] == new_url
    assert history(old_id, db_path).get_messages()[0]["content"] == "from original"
    assert history(new_id, db_path).get_messages()[0]["content"] == "from original"
    assert history(new_id + "_web", db_path).get_messages()[0]["content"] == "from web original"


def test_setup_sessions_fork_failure_keeps_old_id(tmp_path, monkeypatch):
    """A failed fork keeps the old id and leaves the URL unstamped so the next open retries."""
    import hashlib

    import plugin.chatbot.panel_factory

    old_url = "file:///tmp/original.odt"
    new_url = "file:///tmp/copy.odt"
    old_id = hashlib.sha256(old_url.encode("utf-8")).hexdigest()
    props = {"WriterAgentSessionID": old_id}

    def failing_fork(old, new):
        raise OSError("Permission denied")

    monkeypatch.setattr(plugin.chatbot.panel_factory, "_fork_doc_chat_history", failing_fork)

    _save_as_sessions(tmp_path, props, new_url)

    assert props["WriterAgentSessionID"] == old_id
    assert "WriterAgentSessionURL" not in props


def test_setup_sessions_untitled_uuid_keeps_history_on_first_save(tmp_path):
    """The first save of an untitled document keeps its UUID chat."""
    import hashlib
    import uuid

    from plugin.chatbot.history_db import get_chat_history

    new_url = "file:///tmp/first-save.odt"
    session_id = str(uuid.uuid4())
    url_id = hashlib.sha256(new_url.encode("utf-8")).hexdigest()
    props = {"WriterAgentSessionID": session_id}
    db_path = str(tmp_path / "writeragent_history.db")
    get_chat_history(session_id, db_path).add_message("user", "untitled chat")
    history = _save_as_sessions(tmp_path, props, new_url)
    assert props["WriterAgentSessionID"] == session_id
    assert props["WriterAgentSessionURL"] == new_url
    assert history(session_id, db_path).get_messages()[0]["content"] == "untitled chat"
    assert history(url_id, db_path).get_messages() == []


def test_setup_sessions_save_as_copies_once(tmp_path):
    """A real URL change copies history once; a second setup does not duplicate it."""
    import hashlib

    from plugin.chatbot.history_db import get_chat_history

    old_url = "file:///tmp/original.odt"
    new_url = "file:///tmp/copy.odt"
    old_id = hashlib.sha256(old_url.encode("utf-8")).hexdigest()
    new_id = hashlib.sha256(new_url.encode("utf-8")).hexdigest()
    props = {"WriterAgentSessionID": old_id, "WriterAgentSessionURL": old_url}
    db_path = str(tmp_path / "writeragent_history.db")
    get_chat_history(old_id, db_path).add_message("assistant", "kept", [{"id": "call-9"}])
    history = _save_as_sessions(tmp_path, props, new_url)
    assert props["WriterAgentSessionID"] == new_id
    assert props["WriterAgentSessionURL"] == new_url
    copied = history(new_id, db_path).get_messages()
    assert copied == [{"role": "assistant", "content": "kept", "tool_calls": [{"id": "call-9"}]}]
    assert history(old_id, db_path).get_messages() == copied
    history_again = _save_as_sessions(tmp_path, props, new_url)
    assert history_again(new_id, db_path).get_messages() == copied


def test_disposing_swallows_disposed_focus_restore():
    from unittest.mock import MagicMock

    el = _thin_panel_element()
    el.frame_session = MagicMock()
    el.frame_session.release_panel.side_effect = _MockDisposedException("bridge gone")
    el.disposing(None)
    assert el.rich_text_widget is None


def test_disposing_unsubscribes_config_changed():
    from unittest.mock import patch

    el = _thin_panel_element()
    with patch("plugin.framework.event_bus.global_event_bus.unsubscribe") as mock_unsub:
        el.disposing(None)
        mock_unsub.assert_called_with("config:changed", el._on_config_changed)


def test_disposing_swallows_disposed_remove_window_listener():
    from unittest.mock import MagicMock

    el = _thin_panel_element()
    root = MagicMock()
    root.removeWindowListener.side_effect = _MockDisposedException("window gone")
    tp = MagicMock()
    tp.resize_listener = MagicMock()
    el.toolpanel = tp
    el.m_panelRootWindow = root
    el.disposing(None)
    root.removeWindowListener.assert_called_once()


def test_run_on_main_thread_inline_on_vcl():
    from unittest.mock import patch
    from plugin.chatbot.panel_factory import _run_on_main_thread

    with patch("plugin.framework.thread_guard.on_main_thread", return_value=True), patch(
        "plugin.framework.queue_executor.execute_on_main_thread"
    ) as exe:
        assert _run_on_main_thread(lambda: 42) == 42
    exe.assert_not_called()


def test_run_on_main_thread_marshals_off_vcl():
    from unittest.mock import patch
    from plugin.chatbot.panel_factory import _run_on_main_thread

    with patch("plugin.framework.thread_guard.on_main_thread", return_value=False), patch(
        "plugin.framework.queue_executor.execute_on_main_thread",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ) as exe:
        assert _run_on_main_thread(lambda: 42) == 42
    exe.assert_called_once()


def test_initialize_extension_paths_hops_impl_not_self():
    """Dummy-N path init hops the body; must not self-call (TESTING=1 inline)."""
    from unittest.mock import patch
    from plugin.chatbot import panel_factory as pf

    pf._paths_initialized = False
    try:
        with patch.object(pf, "_run_on_main_thread") as hop:
            hop.return_value = None
            pf._initialize_extension_paths(object())
        hop.assert_called_once()
        impl = hop.call_args[0][0]
        assert impl is not pf._initialize_extension_paths
        assert impl.__name__ == "_impl"
    finally:
        pf._paths_initialized = False


def test_initialize_extension_paths_impl_sets_flag_when_hop_inlines():
    from unittest.mock import patch
    from plugin.chatbot import panel_factory as pf

    pf._paths_initialized = False
    try:
        with patch.object(
            pf, "_run_on_main_thread", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)
        ), patch.object(pf, "get_extension_path", return_value="/tmp/ext"), patch.object(
            pf, "init_logging"
        ), patch(
            "plugin.writer.locale.ai_grammar_proofreader.ensure_writeragent_proofreader_configured"
        ):
            pf._initialize_extension_paths(object())
        assert pf._paths_initialized is True
    finally:
        pf._paths_initialized = False


def test_initialize_extension_paths_inline_hop_does_not_recurse():
    """TESTING=1 inlines execute_on_main_thread on Dummy-N; must not stack-overflow."""
    from unittest.mock import patch
    from plugin.chatbot import panel_factory as pf

    pf._paths_initialized = False
    try:
        with patch("plugin.framework.thread_guard.on_main_thread", return_value=False), patch(
            "plugin.framework.queue_executor.execute_on_main_thread",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ), patch.object(pf, "get_extension_path", return_value="/tmp/ext"), patch.object(
            pf, "init_logging"
        ), patch(
            "plugin.writer.locale.ai_grammar_proofreader.ensure_writeragent_proofreader_configured"
        ):
            pf._initialize_extension_paths(object())
        assert pf._paths_initialized is True
    finally:
        pf._paths_initialized = False


def test_panel_root_window_uses_element_ctx():
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock(name="element-ctx")
    el.xParentWindow = MagicMock()
    el.xParentWindow.getPosSize.return_value = MagicMock(Height=400, Width=200)
    window = MagicMock()
    window.getPosSize.return_value = MagicMock(Height=400, Width=800)
    provider = MagicMock()
    provider.createContainerWindow.return_value = window
    el.ctx.getServiceManager.return_value.createInstanceWithContext.return_value = provider

    with patch("plugin.chatbot.panel_factory.get_extension_url", return_value="file:///ext") as url:
        el._getOrCreatePanelRootWindow()

    url.assert_called_once_with(el.ctx)
    create = el.ctx.getServiceManager.return_value.createInstanceWithContext
    create.assert_called_once()
    assert create.call_args[0][1] is el.ctx


def test_release_live_sidebar_keeps_the_newer_window():
    from unittest.mock import MagicMock

    from plugin.chatbot.panel_factory import release_live_sidebar
    from plugin.doc.live_panels import get_live_panel, register_live_panel, reset_live_panels
    from plugin.framework.frame_session import FrameSession, reset_frame_sessions

    reset_live_panels()
    reset_frame_sessions()
    first = _thin_panel_element()
    second = _thin_panel_element()
    first._live_panel_uid = "shared"
    second._live_panel_uid = "shared"
    first_query = MagicMock(name="first-query")
    second_query = MagicMock(name="second-query")
    first.send_listener = MagicMock()
    first_session = FrameSession(MagicMock(name="frame-a"), "shared")
    second_session = FrameSession(MagicMock(name="frame-b"), "shared")
    first.frame_session = first_session
    second.frame_session = second_session
    first_session.bind_panel(first)
    second_session.bind_panel(second)
    first_session.set_focus_pin(first_query)
    second_session.set_focus_pin(second_query)
    register_live_panel("shared", second)
    try:
        release_live_sidebar(first, first_query)
        assert get_live_panel("shared") is second
        assert second_session.focus_pin is second_query
        assert first_session.focus_pin is None
        first.send_listener.disposing.assert_called_once_with(None)
        release_live_sidebar(second, second_query)
        assert get_live_panel("shared") is None
        assert second_session.focus_pin is None
    finally:
        reset_live_panels()
        reset_frame_sessions()


def test_get_real_interface_create_goes_through_main_thread_hop():
    """getRealInterface must hop create so Dummy-N never calls get_extension_url."""
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xParentWindow = MagicMock()
    el.ResourceURL = "private:resource/ChatPanel"
    panel = MagicMock()

    def fake_hop(fn, *args, **kwargs):
        el.toolpanel = panel
        return None

    with patch("plugin.chatbot.panel_factory._run_on_main_thread", side_effect=fake_hop):
        result = el.getRealInterface()
    assert result is panel


def test_get_real_interface_create_runs_path_init_on_hop():
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xParentWindow = MagicMock()
    el.ResourceURL = "private:resource/ChatPanel"
    root = MagicMock()
    panel = MagicMock()

    with patch(
        "plugin.chatbot.panel_factory._run_on_main_thread",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ), patch("plugin.chatbot.panel_factory._initialize_extension_paths") as init, patch.object(
        el, "_getOrCreatePanelRootWindow", return_value=root
    ), patch(
        "plugin.chatbot.panel_factory.ChatToolPanel", return_value=panel
    ), patch(
        "plugin.chatbot.panel_factory.wire_chatpanel_controls"
    ):
        result = el.getRealInterface()
    init.assert_called_once_with(el.ctx)
    assert result is panel


def test_get_real_interface_skips_hop_when_panel_exists():
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    existing = MagicMock()
    el.toolpanel = existing
    with patch("plugin.chatbot.panel_factory._run_on_main_thread") as hop:
        result = el.getRealInterface()
    hop.assert_not_called()
    assert result is existing


def test_sidebar_model_selectors_skip_remote_fetch():
    """Sidebar create and config:changed must not fetch the catalog on the UI thread."""
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el._in_refresh_controls = False
    el.m_panelRootWindow = MagicMock()
    el._update_backend_indicator = MagicMock()
    model_selector = MagicMock()
    image_selector = MagicMock()

    def get_optional(_root, name):
        if name == "model_selector":
            return model_selector
        if name == "image_model_selector":
            return image_selector
        return None

    with patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru", return_value="m") as pop, \
         patch("plugin.chatbot.config_ui_helpers.populate_image_model_selector", return_value="img") as image_pop, \
         patch("plugin.chatbot.panel_factory.get_text_model", return_value="m"), \
         patch("plugin.chatbot.panel_factory.get_image_model", return_value="img"), \
         patch("plugin.chatbot.panel_factory.get_config", return_value=""), \
         patch("plugin.chatbot.panel_factory.get_current_endpoint", return_value="http://127.0.0.1:9"), \
         patch("plugin.chatbot.panel_factory.get_optional_control", side_effect=get_optional):
        el._wire_model_selectors(model_selector, image_selector)
        el._refresh_controls_from_config()

    assert pop.call_count == 2
    assert image_pop.call_count == 2
    for call in pop.call_args_list:
        assert call.kwargs.get("skip_remote_fetch") is True
        assert "model_lru" in call.args
    for call in image_pop.call_args_list:
        assert call.kwargs.get("skip_remote_fetch") is True


def test_refresh_mode_selector_disposed_still_updates_backend_indicator():
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el._in_refresh_controls = False
    root = MagicMock()

    def get_control(name):
        if name == "chat_mode_selector":
            return MagicMock()
        return None

    root.getControl.side_effect = get_control
    el.m_panelRootWindow = root
    el._update_backend_indicator = MagicMock()
    with patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru"), patch(
        "plugin.chatbot.config_ui_helpers.populate_image_model_selector"
    ), patch("plugin.chatbot.panel_factory.get_text_model", return_value="m"), patch(
        "plugin.chatbot.panel_factory.get_config", return_value=""
    ), patch(
        "plugin.chatbot.panel_factory.get_current_endpoint", return_value=""
    ), patch(
        "plugin.chatbot.panel_factory.get_optional_control",
        side_effect=lambda _root, name: MagicMock() if name == "chat_mode_selector" else None,
    ), patch.object(
        el, "_get_document_model", side_effect=_MockDisposedException("model disposed")
    ):
        el._refresh_controls_from_config()
    el._update_backend_indicator.assert_called_once_with(root)


def test_refresh_controls_syncs_chk_voice_from_tts_enabled():
    """Settings → Speech TTS toggle must refresh the sidebar Voice checkbox."""
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el._in_refresh_controls = False
    root = MagicMock()
    el.m_panelRootWindow = root
    el._update_backend_indicator = MagicMock()

    chk = MagicMock()
    chk.getState.return_value = 0  # sidebar stale (off)
    chk.setState = MagicMock()

    def get_optional(_root, name):
        if name == "chk_voice":
            return chk
        return None

    with patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru"), patch(
        "plugin.chatbot.config_ui_helpers.populate_image_model_selector"
    ), patch("plugin.chatbot.panel_factory.get_text_model", return_value="m"), patch(
        "plugin.chatbot.panel_factory.get_config", return_value=""
    ), patch(
        "plugin.chatbot.panel_factory.get_current_endpoint", return_value=""
    ), patch(
        "plugin.chatbot.panel_factory.get_optional_control",
        side_effect=get_optional,
    ), patch(
        "plugin.framework.config.get_config_bool_safe",
        side_effect=lambda key: True if key == "audio.tts_enabled" else False,
    ):
        el._refresh_controls_from_config()

    chk.setState.assert_called_once_with(1)
    el._update_backend_indicator.assert_called_once_with(root)


def test_refresh_controls_skips_chk_voice_setstate_when_already_matched():
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el._in_refresh_controls = False
    root = MagicMock()
    el.m_panelRootWindow = root
    el._update_backend_indicator = MagicMock()

    chk = MagicMock()
    chk.getState.return_value = 1
    chk.setState = MagicMock()

    def get_optional(_root, name):
        if name == "chk_voice":
            return chk
        return None

    with patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru"), patch(
        "plugin.chatbot.config_ui_helpers.populate_image_model_selector"
    ), patch("plugin.chatbot.panel_factory.get_text_model", return_value="m"), patch(
        "plugin.chatbot.panel_factory.get_config", return_value=""
    ), patch(
        "plugin.chatbot.panel_factory.get_current_endpoint", return_value=""
    ), patch(
        "plugin.chatbot.panel_factory.get_optional_control",
        side_effect=get_optional,
    ), patch(
        "plugin.framework.config.get_config_bool_safe",
        side_effect=lambda key: True if key == "audio.tts_enabled" else False,
    ):
        el._refresh_controls_from_config()

    chk.setState.assert_not_called()


def test_header_third_button_is_writer_latex_calc_python_else_hidden():
    from unittest.mock import patch

    from plugin.chatbot.panel_factory import header_third_button_kind

    with patch("plugin.doc.doc_type.is_calc", side_effect=lambda model: model == "calc"), patch(
        "plugin.doc.doc_type.is_writer", side_effect=lambda model: model == "writer"
    ):
        assert header_third_button_kind("writer") == "latex"
        assert header_third_button_kind("calc") == "python_cell"
        assert header_third_button_kind("draw") == ""
        assert header_third_button_kind("impress") == ""


def test_refresh_restores_mode_captured_before_populate():
    """populate resets the combo to Chat. Refresh must put the prior mode back."""
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_LIBRARIAN, sidebar_mode_flags_for_doc_type

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el._in_refresh_controls = False
    el.m_panelRootWindow = MagicMock()
    el._update_backend_indicator = MagicMock()
    el._get_document_model = MagicMock(return_value=MagicMock())
    el.send_listener = MagicMock()
    el.send_listener.cached_doc_type = "writer"
    selector = MagicMock()
    flags = sidebar_mode_flags_for_doc_type("writer")

    def get_optional(_root, name):
        if name == "chat_mode_selector":
            return selector
        return None

    with patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru"), patch(
        "plugin.chatbot.config_ui_helpers.populate_image_model_selector"
    ), patch("plugin.chatbot.panel_factory.get_text_model", return_value="m"), patch(
        "plugin.chatbot.panel_factory.get_config", return_value=""
    ), patch(
        "plugin.chatbot.panel_factory.get_current_endpoint", return_value=""
    ), patch(
        "plugin.chatbot.panel_factory.get_optional_control",
        side_effect=get_optional,
    ), patch(
        "plugin.chatbot.chat_sidebar_mode.mode_from_selector_with_flags",
        return_value=CHAT_MODE_LIBRARIAN,
    ) as read_mode, patch(
        "plugin.chatbot.chat_sidebar_mode.populate_mode_selector_with_flags",
    ) as populate, patch(
        "plugin.chatbot.chat_sidebar_mode.set_selector_mode_with_flags",
    ) as restore:
        order: list[str] = []
        read_mode.side_effect = lambda *_args, **_kwargs: order.append("read") or CHAT_MODE_LIBRARIAN
        populate.side_effect = lambda *_args, **_kwargs: order.append("populate")
        restore.side_effect = lambda *_args, **_kwargs: order.append("restore")
        el._refresh_controls_from_config()

    assert order == ["read", "populate", "restore"]
    read_mode.assert_called_once_with(selector, flags)
    populate.assert_called_once_with(selector, flags)
    restore.assert_called_once_with(selector, CHAT_MODE_LIBRARIAN, flags)


def test_chat_mode_listener_ignores_refresh():
    from unittest.mock import MagicMock

    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el._in_refresh_controls = True
    el._apply_sidebar_mode = MagicMock()
    selector = MagicMock()
    selector.getSelectedItemPos.side_effect = RuntimeError("disposed during refresh")
    selector.getText.return_value = "Chat"
    selector.addItemListener = MagicMock()
    flags = SidebarModeFlags()
    el._wire_chat_mode_listener(selector, MagicMock(), None, None, None, lambda _mode: None, flags)
    listener = selector.addItemListener.call_args[0][0]
    listener.on_item_state_changed(None)
    el._apply_sidebar_mode.assert_not_called()

    el._in_refresh_controls = False
    listener.on_item_state_changed(None)
    el._apply_sidebar_mode.assert_called_once()


def test_chat_mode_listener_ignores_combo_while_send_is_busy():
    from unittest.mock import MagicMock

    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el._in_refresh_controls = False
    el._apply_sidebar_mode = MagicMock()
    selector = MagicMock()
    selector.getSelectedItemPos.return_value = 0
    selector.getText.return_value = "Librarian"
    selector.addItemListener = MagicMock()
    send = MagicMock()
    send.sidebar_state.send.is_busy = True
    flags = SidebarModeFlags()
    el._wire_chat_mode_listener(selector, MagicMock(), None, send, None, lambda _mode: None, flags)
    listener = selector.addItemListener.call_args[0][0]
    listener.on_item_state_changed(None)
    el._apply_sidebar_mode.assert_not_called()


def test_render_session_history_logs_greeting_argument() -> None:
    """The failure log must include the greeting, not a literal %s."""
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.rich_text_widget = None
    response = MagicMock()
    response.getModel.side_effect = RuntimeError("render boom")

    with patch("plugin.chatbot.panel_factory.log") as factory_log:
        el._render_session_history(MagicMock(), response, MagicMock(), greeting="Hello there")

    factory_log.exception.assert_called_once_with("_render_session_history failed [greeting=%s]", "Hello there")


def test_wire_buttons_missing_mode_flags_still_attaches_send_and_stop() -> None:
    """None mode_flags must not AttributeError inside the Send/Stop try."""
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xFrame = MagicMock()
    el.session = MagicMock()
    el.frame_session = None
    el._live_panel_uid = "uid"
    el._apply_sidebar_mode = MagicMock()
    el._greeting_for_sidebar_mode = MagicMock(return_value="")
    send = MagicMock()
    stop = MagicMock()
    selector = MagicMock()
    controls = {
        "send": send,
        "stop": stop,
        "query": MagicMock(),
        "response": MagicMock(),
        "image_model_selector": None,
        "model_selector": None,
        "status": None,
        "chat_mode_selector": selector,
        "aspect_ratio_selector": None,
        "base_size_input": None,
        "clear": None,
        "chk_voice": None,
        "btn_settings": None,
        "btn_python": None,
        "btn_latex": None,
        "btn_search": None,
        "btn_hamburger": None,
    }
    sentinel = MagicMock(name="send_listener")
    toggle = MagicMock()

    with (
        patch("plugin.chatbot.panel_factory.header_third_button_kind", return_value=""),
        patch("plugin.framework.uno_context.get_extension_url", return_value=""),
        patch("plugin.framework.menu_icon_dpi.menu_icon_asset_rel", return_value="assets/gear_16.png"),
        patch("plugin.chatbot.panel.SendButtonListener", return_value=sentinel) as ctor,
        patch("plugin.chatbot.panel_factory.register_debug_live_panel"),
        patch("plugin.doc.live_panels.register_live_panel"),
        patch("plugin.doc.doc_type.get_document_type", return_value=MagicMock()),
        patch("plugin.doc.doc_type.doc_type_label_for_enum", return_value="writer"),
        patch("plugin.doc.doc_type.doc_type_title_for_label", return_value="Writer"),
        patch("plugin.doc.doc_type.get_document_uno_services", return_value=frozenset()),
        patch("plugin.chatbot.panel_factory.start_watchdog_thread"),
        patch("plugin.chatbot.panel.StopButtonListener", return_value=MagicMock()),
    ):
        el._wire_buttons(controls, MagicMock(), "chat", None, toggle)

    assert ctor.call_args.kwargs["sidebar_include_brainstorming"] is False
    send.addActionListener.assert_called_once_with(sentinel)
    stop.addActionListener.assert_called_once()
    assert isinstance(sentinel.sidebar_mode_flags, SidebarModeFlags)
    listener = selector.addItemListener.call_args[0][0]
    assert isinstance(listener.mode_flags, SidebarModeFlags)
    el._apply_sidebar_mode.assert_called_once()
    assert el._apply_sidebar_mode.call_args.args[-1] is toggle


def test_fork_doc_chat_history_ignores_system_only_destination(tmp_path, monkeypatch):
    import plugin.chatbot.history_db
    from plugin.chatbot.history_db import get_chat_history
    from plugin.chatbot.panel_factory import _fork_doc_chat_history

    db_path = str(tmp_path / "writeragent_history.db")
    monkeypatch.setattr(plugin.chatbot.history_db, "_get_db_path", lambda: db_path)

    get_chat_history("test-old").add_message("user", "this should copy")
    # Clear seeds a system prompt; that alone is not a conversation to preserve.
    get_chat_history("test-new").add_message("system", "seeded prompt")

    _fork_doc_chat_history("test-old", "test-new")

    msgs = get_chat_history("test-new").get_messages()
    assert [m["content"] for m in msgs] == ["this should copy"]


def test_setup_sessions_save_as_existing_target_preserves_destination_chat(tmp_path):
    """Save As over an existing file with its own history must not overwrite that history."""
    import hashlib

    from plugin.chatbot.history_db import get_chat_history

    old_url = "file:///tmp/source.odt"
    new_url = "file:///tmp/target.odt"
    old_id = hashlib.sha256(old_url.encode("utf-8")).hexdigest()
    new_id = hashlib.sha256(new_url.encode("utf-8")).hexdigest()
    props = {"WriterAgentSessionID": old_id, "WriterAgentSessionURL": old_url}
    db_path = str(tmp_path / "writeragent_history.db")

    # Source chat
    get_chat_history(old_id, db_path).add_message("user", "source chat")
    # Pre-existing target chat
    get_chat_history(new_id, db_path).add_message("user", "pre-existing target chat")

    history = _save_as_sessions(tmp_path, props, new_url)

    # The source chat must stay intact
    assert history(old_id, db_path).get_messages()[0]["content"] == "source chat"
    # The target chat must not be overwritten by the source chat
    assert history(new_id, db_path).get_messages()[0]["content"] == "pre-existing target chat"
    assert len(history(new_id, db_path).get_messages()) == 1

def test_module_logger_name():
    import plugin.chatbot.panel_factory
    assert plugin.chatbot.panel_factory.log.name.startswith("plugin.")
