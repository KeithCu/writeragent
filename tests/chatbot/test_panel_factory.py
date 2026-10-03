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


def test_disposing_swallows_disposed_focus_restore():
    from unittest.mock import MagicMock

    el = _thin_panel_element()
    el.frame_session = MagicMock()
    el.frame_session.release_panel.side_effect = _MockDisposedException("bridge gone")
    el.disposing(None)
    assert el.rich_text_widget is None


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
