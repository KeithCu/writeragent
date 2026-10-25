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
    el._released = False
    el._in_refresh_controls = False
    el.frame_session = None
    el.doc_session = None
    el.web_session = None
    el.librarian_session = None
    el._panel_resize_listener = None
    el._rich_control_listener = None
    el.xFrame = None
    el._last_mode_labels = None
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
        patch("plugin.chatbot.chat_session.ChatSession", return_value=MagicMock()),
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
        patch("plugin.chatbot.chat_session.ChatSession", return_value=MagicMock()),
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

    import plugin.chatbot.history_db

    old_url = "file:///tmp/original.odt"
    new_url = "file:///tmp/copy.odt"
    old_id = hashlib.sha256(old_url.encode("utf-8")).hexdigest()
    props = {"WriterAgentSessionID": old_id}

    def failing_fork(old, new):
        raise OSError("Permission denied")

    monkeypatch.setattr(plugin.chatbot.history_db, "fork_doc_chat_history", failing_fork)

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


def test_initialize_extension_paths_hops_impl_not_self():
    """Dummy-N path init hops the body; must not self-call (TESTING=1 inline)."""
    from unittest.mock import patch
    from plugin.chatbot import panel_factory as pf

    pf._paths_initialized = False
    try:
        with patch.object(pf, "run_on_main") as hop:
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
            pf, "run_on_main", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)
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
            side_effect=lambda fn, *args, timeout=None, bound_scope=None, **kwargs: fn(*args, **kwargs),
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

    with patch("plugin.chatbot.panel_factory.run_on_main", side_effect=fake_hop):
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
        "plugin.chatbot.panel_factory.run_on_main",
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
    with patch("plugin.chatbot.panel_factory.run_on_main") as hop:
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
    selector.getSelectedItemPos.return_value = 0
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
        patch("plugin.chatbot.chat_sidebar_mode.greeting_for_sidebar_mode", return_value=""),
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

    assert "sidebar_include_brainstorming" not in ctor.call_args.kwargs
    send.addActionListener.assert_called_once_with(sentinel)
    stop.addActionListener.assert_called_once()
    assert isinstance(sentinel.sidebar_mode_flags, SidebarModeFlags)
    listener = selector.addItemListener.call_args[0][0]
    assert isinstance(listener.mode_flags, SidebarModeFlags)
    el._apply_sidebar_mode.assert_called_once()
    assert el._apply_sidebar_mode.call_args.args[-1] is toggle


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
    # Tests import these as plugin.*, so __name__ would pass a name check here.
    # LibreOffice loads them as UNO components where __name__ is not under
    # plugin, so the source must use the explicit names.
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel, name in (
        ("plugin/chatbot/panel_factory.py", "plugin.chatbot.panel_factory"),
        ("plugin/librepy/panel_factory.py", "plugin.librepy.panel_factory"),
    ):
        src = (root / rel).read_text(encoding="utf-8")
        assert "getLogger(__name__)" not in src, rel
        assert 'log = logging.getLogger("%s")' % name in src, rel


def test_get_real_interface_failure_retry_and_disposing() -> None:
    """A retry after getRealInterface failure must reset _released so disposing runs cleanup."""
    from unittest.mock import MagicMock, patch

    from plugin.framework.errors import UnoObjectError

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xParentWindow = MagicMock()
    el.ResourceURL = "private:resource/ChatPanel"
    el._released = False
    frame_session = MagicMock()
    el.frame_session = frame_session
    panel = MagicMock()

    attempts = 0

    def fake_wire(*_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first build fails")

    with (
        patch("plugin.chatbot.panel_factory.run_on_main", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.chatbot.panel_factory._initialize_extension_paths"),
        patch.object(el, "_getOrCreatePanelRootWindow", return_value=MagicMock()),
        patch("plugin.chatbot.panel_factory.ChatToolPanel", return_value=panel),
        patch("plugin.chatbot.panel_factory.wire_chatpanel_controls", side_effect=fake_wire),
    ):
        import pytest

        with pytest.raises(UnoObjectError):
            el.getRealInterface()
        assert el._released is True
        assert el.toolpanel is None
        assert frame_session.release_panel.call_count == 1

        # Retry must reset _released to False and succeed
        result = el.getRealInterface()
        assert result is panel
        assert el._released is False

    # Disposing must perform cleanup and release frame session
    with (
        patch("plugin.framework.event_bus.global_event_bus.unsubscribe"),
        patch("plugin.doc.live_panels.unregister_live_panel") as unreg_mock,
    ):
        el._live_panel_uid = "doc-uid-1"
        el.disposing()
        assert el._released is True
        assert frame_session.release_panel.call_count == 2
        unreg_mock.assert_called_once_with("doc-uid-1", el)

        # Second disposing call short-circuits due to _released latch
        el.disposing()
        assert frame_session.release_panel.call_count == 2
        assert unreg_mock.call_count == 1


def test_chat_mode_listener_reverts_selector_when_busy() -> None:
    """Selecting a new mode while busy must revert the combo box to the active mode without desync."""
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.doc_session = MagicMock(name="doc_session")
    el.web_session = MagicMock(name="web_session")
    el.librarian_session = MagicMock(name="librarian_session")
    el.session = el.doc_session
    el._current_mode = "chat"
    el._apply_sidebar_mode = MagicMock()

    selector = MagicMock()
    send_listener = MagicMock()
    send_listener.sidebar_state.send.is_busy = True
    mode_flags = SidebarModeFlags()

    with (
        patch("plugin.chatbot.chat_sidebar_mode.greeting_for_sidebar_mode", return_value=""),
        patch("plugin.chatbot.chat_sidebar_mode.mode_from_selector_with_flags", return_value="web"),
        patch("plugin.chatbot.chat_sidebar_mode.set_selector_mode_with_flags") as revert_mock,
    ):
            el._wire_chat_mode_listener(
                selector, MagicMock(), MagicMock(), send_listener, MagicMock(), MagicMock(), mode_flags
            )
            mode_listener = selector.addItemListener.call_args[0][0]

            # When busy: item state changed must revert selector to current applied mode ("chat")
            mode_listener.on_item_state_changed(MagicMock())
            revert_mock.assert_called_once_with(selector, "chat", mode_flags)
            el._apply_sidebar_mode.assert_not_called()

            # When not busy: item state changed applies the new mode
            send_listener.sidebar_state.send.is_busy = False
            mode_listener.on_item_state_changed(MagicMock())
            el._apply_sidebar_mode.assert_called_once()
            assert el._apply_sidebar_mode.call_args[0][0] == "web"


def test_queued_config_refresh_after_disposing_noops() -> None:
    """A config refresh callback queued before teardown must no-op after disposal."""
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    root = MagicMock()
    root.getControl.side_effect = AssertionError("getControl must not be called after disposal")
    el.m_panelRootWindow = root
    el.send_listener = None
    el.frame_session = None

    with (
        patch("plugin.framework.event_bus.global_event_bus.unsubscribe"),
        patch("plugin.doc.live_panels.unregister_live_panel"),
    ):
        el.disposing()
    assert el._released is True

    # Calling refresh directly (as if dequeued from main thread) must return early without touching controls
    el._refresh_controls_from_config()

    # Calling _on_config_changed while released must not post to main thread
    with patch("plugin.framework.queue_executor.post_to_main_thread") as post_mock:
        with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
            el._on_config_changed()
            post_mock.assert_not_called()


def test_wire_buttons_get_document_uno_services_raising_attaches_listeners() -> None:
    """If get_document_uno_services raises, Send/Stop listeners must still be attached."""
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xFrame = MagicMock()
    el.session = MagicMock()
    el.frame_session = None
    el._live_panel_uid = "uid"
    el._apply_sidebar_mode = MagicMock()

    send = MagicMock()
    stop = MagicMock()
    controls = {
        "send": send,
        "stop": stop,
        "query": MagicMock(),
        "response": MagicMock(),
        "image_model_selector": None,
        "model_selector": None,
        "status": None,
        "chat_mode_selector": None,
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

    with (
        patch("plugin.chatbot.chat_sidebar_mode.greeting_for_sidebar_mode", return_value=""),
        patch("plugin.chatbot.panel_factory.header_third_button_kind", return_value=""),
        patch("plugin.framework.uno_context.get_extension_url", return_value=""),
        patch("plugin.framework.menu_icon_dpi.menu_icon_asset_rel", return_value="assets/gear_16.png"),
        patch("plugin.chatbot.panel.SendButtonListener", return_value=sentinel),
        patch("plugin.chatbot.panel_factory.register_debug_live_panel"),
        patch("plugin.doc.live_panels.register_live_panel"),
        patch("plugin.doc.doc_type.get_document_type", return_value=MagicMock()),
        patch("plugin.doc.doc_type.doc_type_label_for_enum", return_value="writer"),
        patch("plugin.doc.doc_type.doc_type_title_for_label", return_value="Writer"),
        patch("plugin.doc.doc_type.get_document_uno_services", side_effect=RuntimeError("services fail")),
        patch("plugin.chatbot.panel_factory.start_watchdog_thread"),
        patch("plugin.chatbot.panel.StopButtonListener", return_value=MagicMock()),
    ):
        el._wire_buttons(controls, MagicMock(), "chat", SidebarModeFlags(), MagicMock())

    send.addActionListener.assert_called_once_with(sentinel)
    stop.addActionListener.assert_called_once()
    assert sentinel.cached_doc_type == "writer"
    assert sentinel.initial_doc_type == "Writer"
    assert sentinel.cached_uno_services == frozenset()


def test_wire_buttons_doc_type_probe_failure_stores_unknown() -> None:
    """If get_document_type raises before storing a label, cached_doc_type must be unknown."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch


    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xFrame = MagicMock()
    el.session = MagicMock()
    el.frame_session = None
    el._live_panel_uid = "uid"
    el._apply_sidebar_mode = MagicMock()
    el._doc_type_label = "unknown"
    el._doc_type_title = "Unknown"
    el._doc_uno_services = frozenset()

    send = MagicMock()
    stop = MagicMock()
    controls = {
        "send": send,
        "stop": stop,
        "query": MagicMock(),
        "response": MagicMock(),
        "image_model_selector": None,
        "model_selector": None,
        "status": None,
        "chat_mode_selector": None,
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
    sentinel = SimpleNamespace(
        cached_doc_type=None,
        initial_doc_type=None,
        cached_uno_services=None,
        sidebar_mode_flags=None,
        _set_button_states=MagicMock(),
    )

    with (
        patch("plugin.chatbot.chat_sidebar_mode.greeting_for_sidebar_mode", return_value=""),
        patch("plugin.chatbot.panel_factory.header_third_button_kind", return_value=""),
        patch("plugin.framework.uno_context.get_extension_url", return_value=""),
        patch("plugin.framework.menu_icon_dpi.menu_icon_asset_rel", return_value="assets/gear_16.png"),
        patch("plugin.chatbot.panel.SendButtonListener", return_value=sentinel),
        patch("plugin.chatbot.panel_factory.register_debug_live_panel"),
        patch("plugin.doc.live_panels.register_live_panel"),
        patch("plugin.doc.doc_type.get_document_type", side_effect=RuntimeError("probe failed")),
        patch("plugin.chatbot.panel_factory.start_watchdog_thread"),
        patch("plugin.chatbot.panel.StopButtonListener", return_value=MagicMock()),
    ):
        mode_flags = el._sidebar_mode_flags(MagicMock())
        el._wire_buttons(controls, MagicMock(), "chat", mode_flags, MagicMock())

    send.addActionListener.assert_called_once_with(sentinel)
    stop.addActionListener.assert_called_once()
    assert sentinel.cached_doc_type == "unknown"
    assert sentinel.initial_doc_type == "Unknown"
    assert sentinel.cached_uno_services == frozenset()


def test_minimal_width_is_pre_negotiation_constant() -> None:
    from typing import Any, cast
    from plugin.chatbot.panel_factory import ChatToolPanel, _PRE_NEGOTIATION_PANEL_WIDTH

    assert ChatToolPanel.getMinimalWidth(cast("Any", object())) == _PRE_NEGOTIATION_PANEL_WIDTH == 320


def test_refresh_controls_continues_after_model_read_failure():
    """Failure to read text model must not skip remaining control refreshes."""
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el._in_refresh_controls = False
    root = MagicMock()
    el.m_panelRootWindow = root
    el._update_backend_indicator = MagicMock()

    chk = MagicMock()
    chk.getState.return_value = 0
    chk.setState = MagicMock()

    def get_optional(_root, name):
        if name == "chk_voice":
            return chk
        return None

    with patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru"), patch(
        "plugin.chatbot.config_ui_helpers.populate_image_model_selector"
    ), patch(
        "plugin.chatbot.panel_factory.get_text_model", side_effect=RuntimeError("model read fail")
    ), patch(
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

    assert not el._in_refresh_controls
    chk.setState.assert_called_once_with(1)
    el._update_backend_indicator.assert_called_once_with(root)


def test_setup_sessions_session_id_write_failure_keeps_old_id(tmp_path):
    import hashlib
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.history_db import get_chat_history

    old_url = "file:///tmp/original.odt"
    new_url = "file:///tmp/copy.odt"
    old_id = hashlib.sha256(old_url.encode("utf-8")).hexdigest()
    props = {
        "WriterAgentSessionID": old_id,
        "WriterAgentSessionURL": old_url,
    }

    el = _thin_panel_element()
    el.ctx = object()
    model = MagicMock()
    model.getURL.return_value = new_url
    db_path = str(tmp_path / "writeragent_history.db")

    def _get(_model, name, default=None):
        return props.get(name, default)

    def _set(_model, name, value):
        if name == "WriterAgentSessionID":
            raise RuntimeError("id write failed")
        props[name] = value

    def _history(session_id, path=None):
        return get_chat_history(session_id, db_path)

    with (
        patch("plugin.chatbot.panel_factory.get_chat_system_prompt_for_document", return_value="SEEDED"),
        patch("plugin.chatbot.panel_factory.get_document_property", side_effect=_get),
        patch("plugin.chatbot.panel_factory.set_document_property", side_effect=_set),
        patch("plugin.chatbot.history_db.get_chat_history", side_effect=_history),
        patch("plugin.chatbot.chat_session.ChatSession") as mock_chat_session,
    ):
        el._setup_sessions(model, "")

    assert props["WriterAgentSessionID"] == old_id
    assert props["WriterAgentSessionURL"] == old_url
    assert mock_chat_session.call_args_list[0].kwargs["session_id"] == old_id


def test_setup_sessions_session_url_write_failure_keeps_new_id(tmp_path):
    import hashlib
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.history_db import get_chat_history

    old_url = "file:///tmp/original.odt"
    new_url = "file:///tmp/copy.odt"
    old_id = hashlib.sha256(old_url.encode("utf-8")).hexdigest()
    new_id = hashlib.sha256(new_url.encode("utf-8")).hexdigest()
    props = {
        "WriterAgentSessionID": old_id,
        "WriterAgentSessionURL": old_url,
    }

    el = _thin_panel_element()
    el.ctx = object()
    model = MagicMock()
    model.getURL.return_value = new_url
    db_path = str(tmp_path / "writeragent_history.db")

    def _get(_model, name, default=None):
        return props.get(name, default)

    def _failing_set(_model, name, value):
        if name == "WriterAgentSessionURL":
            raise RuntimeError("url write failed")
        props[name] = value

    def _successful_set(_model, name, value):
        props[name] = value

    def _history(session_id, path=None):
        return get_chat_history(session_id, db_path)

    with (
        patch("plugin.chatbot.panel_factory.get_chat_system_prompt_for_document", return_value="SEEDED"),
        patch("plugin.chatbot.panel_factory.get_document_property", side_effect=_get),
        patch("plugin.chatbot.panel_factory.set_document_property", side_effect=_failing_set),
        patch("plugin.chatbot.history_db.get_chat_history", side_effect=_history),
        patch("plugin.chatbot.chat_session.ChatSession") as mock_chat_session,
    ):
        el._setup_sessions(model, "")

    assert props["WriterAgentSessionID"] == new_id
    assert props["WriterAgentSessionURL"] == old_url
    assert mock_chat_session.call_args_list[0].kwargs["session_id"] == new_id

    # Call _setup_sessions again with a setter that writes every property.
    # After the second call, WriterAgentSessionURL is the new URL and
    # WriterAgentSessionID is unchanged. That locks the fork_to == session_id self-heal.
    mock_chat_session.reset_mock()
    with (
        patch("plugin.chatbot.panel_factory.get_chat_system_prompt_for_document", return_value="SEEDED"),
        patch("plugin.chatbot.panel_factory.get_document_property", side_effect=_get),
        patch("plugin.chatbot.panel_factory.set_document_property", side_effect=_successful_set),
        patch("plugin.chatbot.history_db.get_chat_history", side_effect=_history),
        patch("plugin.chatbot.chat_session.ChatSession") as mock_chat_session,
    ):
        el._setup_sessions(model, "")

    assert props["WriterAgentSessionURL"] == new_url
    assert props["WriterAgentSessionID"] == new_id


def test_get_real_interface_second_queued_create_keeps_first_panel():
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xParentWindow = MagicMock()
    el.ResourceURL = "private:resource/ChatPanel"
    root = MagicMock()
    panel = MagicMock()

    def run_twice(fn, *args, **kwargs):
        fn(*args, **kwargs)
        fn(*args, **kwargs)

    with (
        patch("plugin.chatbot.panel_factory.run_on_main", side_effect=run_twice),
        patch("plugin.chatbot.panel_factory._initialize_extension_paths"),
        patch.object(el, "_getOrCreatePanelRootWindow", return_value=root),
        patch("plugin.chatbot.panel_factory.ChatToolPanel", return_value=panel) as mock_panel,
        patch("plugin.chatbot.panel_factory.wire_chatpanel_controls") as mock_wire,
    ):
        result = el.getRealInterface()

    mock_panel.assert_called_once()
    mock_wire.assert_called_once()
    assert result is panel
    assert el.toolpanel is panel


def test_wire_buttons_send_constructor_failure_clears_stale_listener():
    from unittest.mock import MagicMock, patch
    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xFrame = MagicMock()
    el.session = MagicMock()
    el.frame_session = None
    el._live_panel_uid = "uid"
    el._apply_sidebar_mode = MagicMock()

    stale_listener = MagicMock(name="stale_send_listener")
    el.send_listener = stale_listener

    send = MagicMock()
    stop = MagicMock()
    controls = {
        "send": send,
        "stop": stop,
        "query": MagicMock(),
        "response": MagicMock(),
        "image_model_selector": None,
        "model_selector": None,
        "status": None,
        "chat_mode_selector": None,
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

    import pytest

    with (
        patch("plugin.chatbot.chat_sidebar_mode.greeting_for_sidebar_mode", return_value=""),
        patch("plugin.chatbot.panel_factory.header_third_button_kind", return_value=""),
        patch("plugin.framework.uno_context.get_extension_url", return_value=""),
        patch("plugin.framework.menu_icon_dpi.menu_icon_asset_rel", return_value="assets/gear_16.png"),
        patch("plugin.chatbot.panel.SendButtonListener", side_effect=RuntimeError("constructor fail")),
        patch("plugin.chatbot.panel_factory.register_debug_live_panel"),
        patch("plugin.doc.live_panels.register_live_panel"),
        patch("plugin.doc.doc_type.get_document_type", return_value=MagicMock()),
        patch("plugin.doc.doc_type.doc_type_label_for_enum", return_value="writer"),
        patch("plugin.doc.doc_type.doc_type_title_for_label", return_value="Writer"),
        patch("plugin.doc.doc_type.get_document_uno_services", return_value=frozenset()),
        patch("plugin.chatbot.panel_factory.start_watchdog_thread"),
        patch("plugin.chatbot.panel.StopButtonListener", return_value=MagicMock()),
        pytest.raises(RuntimeError, match="constructor fail"),
    ):
        el._wire_buttons(controls, MagicMock(), "chat", SidebarModeFlags(), MagicMock())

    assert el.send_listener is None
    send.addActionListener.assert_not_called()


def test_initialize_extension_paths_logging_failure_does_not_escape():
    from unittest.mock import MagicMock, patch
    from plugin.chatbot import panel_factory as pf

    pf._paths_initialized = False
    try:
        mock_init_logging = MagicMock(side_effect=RuntimeError("logging fail"))
        with patch.object(
            pf, "run_on_main", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)
        ), patch.object(pf, "get_extension_path", return_value="/tmp/ext"), patch.object(
            pf, "init_logging", mock_init_logging
        ), patch(
            "plugin.writer.locale.ai_grammar_proofreader.ensure_writeragent_proofreader_configured"
        ):
            pf._initialize_extension_paths(object())
        assert pf._paths_initialized is False
        assert mock_init_logging.call_count == 2
    finally:
        pf._paths_initialized = False


def test_initialize_extension_paths_handler_still_inits_logging():
    from unittest.mock import MagicMock, patch
    from plugin.chatbot import panel_factory as pf

    pf._paths_initialized = False
    try:
        mock_init_logging = MagicMock()
        with patch.object(
            pf, "run_on_main", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)
        ), patch.object(pf, "get_extension_path", side_effect=RuntimeError("path fail")), patch.object(
            pf, "init_logging", mock_init_logging
        ):
            pf._initialize_extension_paths(object())
        assert pf._paths_initialized is False
        mock_init_logging.assert_called_once()
    finally:
        pf._paths_initialized = False


def test_plan_session_id_all_eight_cases() -> None:
    import hashlib
    from plugin.chatbot.panel_factory import SessionIdPlan, plan_session_id

    fixed_uuid = "11111111-2222-3333-4444-555555555555"
    url1 = "file:///tmp/doc1.odt"
    hash1 = hashlib.sha256(url1.encode("utf-8")).hexdigest()
    url2 = "file:///tmp/doc2.odt"
    hash2 = hashlib.sha256(url2.encode("utf-8")).hexdigest()

    # 1. no id, url non-empty
    p1 = plan_session_id(None, None, url1, fixed_uuid)
    assert p1 == SessionIdPlan(session_id=hash1, fork_from=None, stamp_url=True)

    # 2. no id, no url
    p2 = plan_session_id(None, None, "", fixed_uuid)
    assert p2 == SessionIdPlan(session_id=fixed_uuid, fork_from=None, stamp_url=False)

    # 3. stored url non-empty and differs from url, and sha256(url) != stored_id
    p3 = plan_session_id("stored_id_x", url1, url2, fixed_uuid)
    assert p3 == SessionIdPlan(session_id=hash2, fork_from="stored_id_x", stamp_url=True)

    # 4. no stored url, url non-empty, stored id is 64-char lowercase hex, differs from sha256(url)
    p4 = plan_session_id(hash1, None, url2, fixed_uuid)
    assert p4 == SessionIdPlan(session_id=hash2, fork_from=hash1, stamp_url=True)

    # 5. hash equals stored id -> fork_from=None, session_id=stored_id, stamp_url=True when url non-empty
    p5a = plan_session_id(hash2, url1, url2, fixed_uuid)
    assert p5a == SessionIdPlan(session_id=hash2, fork_from=None, stamp_url=True)
    p5b = plan_session_id(hash1, None, url1, fixed_uuid)
    assert p5b == SessionIdPlan(session_id=hash1, fork_from=None, stamp_url=True)

    # 6. stored id is not a sha256 hex (UUID), no stored url, url non-empty -> no fork, stamp_url=True
    p6 = plan_session_id("some-uuid-string", None, url1, fixed_uuid)
    assert p6 == SessionIdPlan(session_id="some-uuid-string", fork_from=None, stamp_url=True)

    # 7. stored url equals url -> no fork, stamp_url=False
    p7 = plan_session_id("any_id", url1, url1, fixed_uuid)
    assert p7 == SessionIdPlan(session_id="any_id", fork_from=None, stamp_url=False)

    # 8. stored id set, url empty -> no fork, stamp_url=False
    p8 = plan_session_id("any_id", url1, "", fixed_uuid)
    assert p8 == SessionIdPlan(session_id="any_id", fork_from=None, stamp_url=False)


def test_debug_live_panels_registry_and_iter() -> None:
    import gc
    from unittest.mock import patch
    from plugin.chatbot.panel_factory import (
        _LIVE_CHAT_PANELS,
        iter_debug_live_chat_panels,
        register_debug_live_panel,
        unregister_debug_live_panel,
    )

    class _Dummy:
        pass

    obj = _Dummy()
    register_debug_live_panel(obj)
    assert obj in _LIVE_CHAT_PANELS

    with patch("plugin.chatbot.panel_factory._is_testing_env_active", return_value=False):
        assert iter_debug_live_chat_panels() == []
        assert obj in _LIVE_CHAT_PANELS

    with patch("plugin.chatbot.panel_factory._is_testing_env_active", return_value=True):
        assert obj in iter_debug_live_chat_panels()

    unregister_debug_live_panel(obj)
    assert obj not in _LIVE_CHAT_PANELS

    # Weakref collection
    obj2 = _Dummy()
    register_debug_live_panel(obj2)
    assert obj2 in _LIVE_CHAT_PANELS
    del obj2
    gc.collect()
    with patch("plugin.chatbot.panel_factory._is_testing_env_active", return_value=True):
        assert iter_debug_live_chat_panels() == []


def test_wire_buttons_send_add_action_listener_failure_disposes_and_reraises() -> None:
    import pytest
    from unittest.mock import MagicMock, patch
    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xFrame = MagicMock()
    el.session = MagicMock()
    el.frame_session = None
    el._live_panel_uid = "uid"
    el._apply_sidebar_mode = MagicMock()

    send = MagicMock()
    send.addActionListener.side_effect = RuntimeError("send attach failed")
    stop = MagicMock()
    controls = {
        "send": send,
        "stop": stop,
        "query": MagicMock(),
        "response": MagicMock(),
        "image_model_selector": None,
        "model_selector": None,
        "status": None,
        "chat_mode_selector": None,
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

    with (
        patch("plugin.chatbot.chat_sidebar_mode.greeting_for_sidebar_mode", return_value=""),
        patch("plugin.chatbot.panel_factory.header_third_button_kind", return_value=""),
        patch("plugin.framework.uno_context.get_extension_url", return_value=""),
        patch("plugin.framework.menu_icon_dpi.menu_icon_asset_rel", return_value="assets/gear_16.png"),
        patch("plugin.chatbot.panel.SendButtonListener", return_value=sentinel),
        patch("plugin.chatbot.panel_factory.release_live_sidebar") as mock_release,
        patch("plugin.chatbot.panel_factory.register_debug_live_panel"),
        patch("plugin.doc.live_panels.register_live_panel") as mock_reg_live,
        pytest.raises(RuntimeError, match="send attach failed"),
    ):
        el._wire_buttons(controls, MagicMock(), "chat", SidebarModeFlags(), MagicMock())

    sentinel.disposing.assert_called_once_with(None)
    mock_release.assert_called_once_with(el)
    assert el.send_listener is None
    mock_reg_live.assert_not_called()

    # A subsequent wire_buttons succeeds and attaches Stop
    send.addActionListener.side_effect = None
    with (
        patch("plugin.chatbot.chat_sidebar_mode.greeting_for_sidebar_mode", return_value=""),
        patch("plugin.chatbot.panel_factory.header_third_button_kind", return_value=""),
        patch("plugin.framework.uno_context.get_extension_url", return_value=""),
        patch("plugin.framework.menu_icon_dpi.menu_icon_asset_rel", return_value="assets/gear_16.png"),
        patch("plugin.chatbot.panel.SendButtonListener", return_value=sentinel),
        patch("plugin.chatbot.panel_factory.register_debug_live_panel"),
        patch("plugin.doc.live_panels.register_live_panel"),
        patch("plugin.chatbot.panel.StopButtonListener", return_value=MagicMock()),
    ):
        ret = el._wire_buttons(controls, MagicMock(), "chat", SidebarModeFlags(), MagicMock())

    assert ret is sentinel
    stop.addActionListener.assert_called_once()


def test_wire_buttons_stop_failure_aborts_registration() -> None:
    import pytest
    from unittest.mock import MagicMock, patch
    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.xFrame = MagicMock()
    el.session = MagicMock()
    el.frame_session = None
    el._live_panel_uid = "uid"
    el._apply_sidebar_mode = MagicMock()

    send = MagicMock()
    stop = MagicMock()
    stop.addActionListener.side_effect = RuntimeError("stop attach failed")
    controls = {
        "send": send,
        "stop": stop,
        "query": MagicMock(),
        "response": MagicMock(),
        "image_model_selector": None,
        "model_selector": None,
        "status": None,
        "chat_mode_selector": None,
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

    with (
        patch("plugin.chatbot.chat_sidebar_mode.greeting_for_sidebar_mode", return_value=""),
        patch("plugin.chatbot.panel_factory.header_third_button_kind", return_value=""),
        patch("plugin.framework.uno_context.get_extension_url", return_value=""),
        patch("plugin.framework.menu_icon_dpi.menu_icon_asset_rel", return_value="assets/gear_16.png"),
        patch("plugin.chatbot.panel.SendButtonListener", return_value=sentinel),
        patch("plugin.chatbot.panel.StopButtonListener", return_value=MagicMock()),
        patch("plugin.chatbot.panel_factory.release_live_sidebar") as mock_release,
        patch("plugin.chatbot.panel_factory.register_debug_live_panel") as mock_reg_debug,
        patch("plugin.doc.live_panels.register_live_panel") as mock_reg_live,
        pytest.raises(RuntimeError, match="stop attach failed"),
    ):
        el._wire_buttons(controls, MagicMock(), "chat", SidebarModeFlags(), MagicMock())

    sentinel.disposing.assert_called_once_with(None)
    mock_release.assert_called_once_with(el)
    assert el.send_listener is None
    mock_reg_live.assert_not_called()
    mock_reg_debug.assert_not_called()


def test_chat_panel_element_disposing_removes_both_listeners() -> None:
    from unittest.mock import MagicMock

    el = _thin_panel_element()
    root = MagicMock()
    el.m_panelRootWindow = root
    tp = MagicMock()
    rl = MagicMock()
    tp.resize_listener = rl
    el.toolpanel = tp
    rich_l = MagicMock()
    el._rich_control_listener = rich_l

    el.disposing()
    assert root.removeWindowListener.call_count == 2
    root.removeWindowListener.assert_any_call(rl)
    root.removeWindowListener.assert_any_call(rich_l)
    assert el._rich_control_listener is None
    assert tp.resize_listener is None

    # Without rich listener, only resize listener is removed
    root.reset_mock()
    tp.resize_listener = rl
    el._rich_control_listener = None
    el.disposing()
    assert root.removeWindowListener.call_count == 1
    root.removeWindowListener.assert_called_once_with(rl)


def test_refresh_controls_populates_model_combos_without_readding_listeners() -> None:
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.m_panelRootWindow = MagicMock()
    el.send_listener = MagicMock()
    el.send_listener.cached_doc_type = "writer"

    model_sel = MagicMock()
    image_sel = MagicMock()
    controls = {"model_selector": model_sel, "image_model_selector": image_sel}
    el.m_panelRootWindow.getControl.side_effect = lambda n: controls.get(n)

    with (
        patch("plugin.chatbot.panel_factory.get_optional_control", side_effect=lambda _r, n: controls.get(n)),
        patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru") as mock_pop_text,
        patch("plugin.chatbot.config_ui_helpers.populate_image_model_selector") as mock_pop_img,
        patch("plugin.chatbot.panel_factory.set_text_model"),
        patch("plugin.chatbot.panel_factory.set_image_model"),
    ):
        mock_pop_text.return_value = "gpt-4o"
        mock_pop_img.return_value = "dall-e-3"
        el._wire_model_selectors(model_sel, image_sel)
        assert model_sel.addItemListener.call_count == 1
        assert image_sel.addItemListener.call_count == 1
        assert mock_pop_text.call_count == 1
        assert mock_pop_img.call_count == 1

        # Now config refresh: should populate again, but not add listeners
        el._refresh_controls_from_config()
        assert model_sel.addItemListener.call_count == 1
        assert image_sel.addItemListener.call_count == 1
        assert mock_pop_text.call_count == 2
        assert mock_pop_img.call_count == 2


def test_refresh_controls_mode_selector_caching() -> None:
    from unittest.mock import MagicMock, patch

    el = _thin_panel_element()
    el.ctx = MagicMock()
    el.m_panelRootWindow = MagicMock()
    el.send_listener = MagicMock()
    el.send_listener.cached_doc_type = "writer"

    chat_mode = MagicMock()
    chat_mode.getSelectedItemPos.return_value = 1
    controls = {"chat_mode_selector": chat_mode}
    el.m_panelRootWindow.getControl.side_effect = lambda n: controls.get(n)

    with (
        patch("plugin.chatbot.panel_factory.get_optional_control", side_effect=lambda _r, n: controls.get(n)),
        patch("plugin.chatbot.panel_factory.ChatPanelElement._populate_model_combos"),
        patch("plugin.chatbot.panel_factory.ChatPanelElement._update_backend_indicator"),
        patch("plugin.chatbot.chat_sidebar_mode.populate_mode_selector_with_flags") as mock_pop_mode,
        patch("plugin.chatbot.chat_sidebar_mode.mode_from_selector_with_flags", return_value="web"),
        patch("plugin.chatbot.chat_sidebar_mode.set_selector_mode_with_flags"),
    ):
        # First refresh: labels change from None -> ("Chat", "Web", ...) -> populate runs
        el._refresh_controls_from_config()
        assert mock_pop_mode.call_count == 1

        # Second refresh: same doc type ("writer"), same labels -> populate skipped
        el._refresh_controls_from_config()
        assert mock_pop_mode.call_count == 1

        # Third refresh: doc type changes -> labels differ -> populate runs again
        el.send_listener.cached_doc_type = "calc"
        el._refresh_controls_from_config()
        assert mock_pop_mode.call_count == 2



