"""Sidebar combobox populate must not recurse via UNO listeners + config:changed.

VCL fires textChanged/itemStateChanged on programmatic setText/addItems.
Without _in_refresh_controls that re-enters _refresh_controls_from_config
on the main thread (docs/framework/uno-thread-safety.md §12).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from plugin.chatbot.panel_factory import ChatPanelElement
from plugin.framework.event_bus import EventBus
from plugin.framework.uno_listeners import BaseTextListener

_MAX_REFRESH = 20
_MODEL = "openai/gpt-test"
_ENDPOINT = "https://openrouter.ai/api"


class FiringCombo:
    """ComboBox stand-in: mutations synchronously notify listeners like VCL."""

    def __init__(self) -> None:
        self._text = ""
        self._items: list[str] = []
        self._text_listeners: list = []
        self._item_listeners: list = []
        self.set_text_calls = 0

    def addTextListener(self, listener) -> None:
        self._text_listeners.append(listener)

    def addItemListener(self, listener) -> None:
        self._item_listeners.append(listener)

    def getText(self) -> str:
        return self._text

    def getItemCount(self) -> int:
        return len(self._items)

    def getModel(self):
        return object()

    def removeItems(self, start: int, count: int) -> None:
        if count:
            self._items[start : start + count] = []
        self._fire_item()

    def addItems(self, items, pos: int) -> None:
        seq = list(items)
        self._items[pos:pos] = seq
        self._fire_item()

    def setText(self, text: str) -> None:
        self.set_text_calls += 1
        self._text = text
        self._fire_text()

    def _fire_text(self) -> None:
        for listener in list(self._text_listeners):
            listener.textChanged(None)

    def _fire_item(self) -> None:
        for listener in list(self._item_listeners):
            listener.itemStateChanged(None)


def _fake_populate(ctx, ctrl, current_val, lru_key, endpoint, **kwargs):
    value = str(current_val or _MODEL)
    ctrl.removeItems(0, ctrl.getItemCount())
    ctrl.addItems((value,), 0)
    ctrl.setText(value)
    return value


def _make_panel(combo: FiringCombo):
    root = SimpleNamespace(
        getControl=lambda name: combo if name == "model_selector" else (_ for _ in ()).throw(Exception("missing")),
    )
    panel = SimpleNamespace(
        ctx=object(),
        _in_refresh_controls=False,
        m_panelRootWindow=root,
        xFrame=None,
        send_listener=None,
        refresh_count=0,
        _get_document_model=lambda: None,
        _update_backend_indicator=lambda root_window=None: None,
    )
    return panel


def _capped_refresh(panel):
    def _run():
        panel.refresh_count += 1
        if panel.refresh_count > _MAX_REFRESH:
            raise AssertionError("sidebar refresh re-entered more than %s times" % _MAX_REFRESH)
        ChatPanelElement._refresh_controls_from_config(panel)

    return _run


def test_refresh_controls_does_not_recurse_when_combo_fires_listeners():
    combo = FiringCombo()
    panel = _make_panel(combo)
    bus = EventBus()
    set_config_keys: list[str] = []

    def set_config(key, value):
        set_config_keys.append(key)
        bus.emit("config:changed", ctx=None)

    refresh = _capped_refresh(panel)
    bus.subscribe("config:changed", lambda **kwargs: refresh())

    with (
        patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru", side_effect=_fake_populate),
        patch("plugin.chatbot.config_ui_helpers.populate_image_model_selector", return_value=""),
        patch("plugin.chatbot.panel_factory.get_text_model", return_value=_MODEL),
        patch("plugin.chatbot.panel_factory.get_image_model", return_value=""),
        patch("plugin.chatbot.panel_factory.get_current_endpoint", return_value=_ENDPOINT),
        patch("plugin.chatbot.panel_factory.get_config", return_value=""),
        patch("plugin.chatbot.panel_factory.set_text_model"),
        patch("plugin.chatbot.panel_factory.set_image_model"),
        patch("plugin.chatbot.panel_factory.set_control_enabled"),
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.chatbot.config_ui_helpers.get_current_endpoint", return_value=_ENDPOINT),
        patch("plugin.chatbot.config_ui_helpers.get_config", return_value=[]),
        patch("plugin.chatbot.config_ui_helpers.set_config", side_effect=set_config),
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value=_MODEL),
        patch("plugin.framework.client.model_fetcher.set_text_model"),
    ):
        ChatPanelElement._wire_model_selectors(panel, combo, None)
        bus.emit("config:changed")

    assert panel.refresh_count == 1
    assert combo.set_text_calls >= 1
    assert set_config_keys == []


def test_typed_image_model_is_saved_unless_refreshing():
    image = FiringCombo()
    panel = _make_panel(FiringCombo())
    saved: list[str] = []

    def set_image_model(txt, update_lru=False):
        saved.append(txt)

    with (
        patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru", side_effect=_fake_populate),
        patch("plugin.chatbot.config_ui_helpers.populate_image_model_selector", return_value=""),
        patch("plugin.chatbot.panel_factory.get_text_model", return_value=_MODEL),
        patch("plugin.chatbot.panel_factory.get_image_model", return_value=""),
        patch("plugin.chatbot.panel_factory.get_current_endpoint", return_value=_ENDPOINT),
        patch("plugin.chatbot.panel_factory.get_config", return_value=""),
        patch("plugin.chatbot.panel_factory.set_text_model"),
        patch("plugin.chatbot.panel_factory.set_image_model", side_effect=set_image_model),
    ):
        ChatPanelElement._wire_model_selectors(panel, None, image)
        image.setText("vendor/typed-image")
        assert saved == ["vendor/typed-image"]
        panel._in_refresh_controls = True
        image.setText("vendor/during-refresh")
        assert saved == ["vendor/typed-image"]


def test_measure_aux_button_width_uses_translated_labels(monkeypatch):
    from plugin.chatbot.panel_wiring import _measure_aux_button_max_width

    class _Pos:
        def __init__(self, width: int) -> None:
            self.Width = width

    class _Model:
        Label = "Stop"

    model = _Model()

    class _Ctrl:
        def getModel(self):
            return model

        def getPosSize(self):
            return _Pos(len(model.Label))

    monkeypatch.setattr("plugin.chatbot.panel_wiring._", lambda text: text + "XX")
    width = _measure_aux_button_max_width(_Ctrl(), ["Stop", "Change"])
    assert model.Label == "Stop"
    assert width == len("ChangeXX")


def test_unguarded_listener_does_not_infinite_loop_because_event_bus_drops():
    """Without the panel flag, VCL-like setText still writes LRU; bus drop stops the hang.

    Proves the harness would keep calling refresh if both the flag and the bus
    drop were missing; with the bus, refresh stays bounded and set_config runs.
    """
    combo = FiringCombo()
    panel = _make_panel(combo)
    bus = EventBus()
    set_config_keys: list[str] = []

    def set_config(key, value):
        set_config_keys.append(key)
        bus.emit("config:changed", ctx=None)

    refresh = _capped_refresh(panel)
    bus.subscribe("config:changed", lambda **kwargs: refresh())

    class Unguarded(BaseTextListener):
        def on_text_changed(self, rEvent):
            from plugin.chatbot.config_ui_helpers import sync_sidebar_text_model

            sync_sidebar_text_model(panel.ctx, combo)

    combo.addTextListener(Unguarded())

    with (
        patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru", side_effect=_fake_populate),
        patch("plugin.chatbot.config_ui_helpers.populate_image_model_selector", return_value=""),
        patch("plugin.chatbot.panel_factory.get_text_model", return_value=_MODEL),
        patch("plugin.chatbot.panel_factory.get_image_model", return_value=""),
        patch("plugin.chatbot.panel_factory.get_current_endpoint", return_value=_ENDPOINT),
        patch("plugin.chatbot.panel_factory.get_config", return_value=""),
        patch("plugin.chatbot.panel_factory.set_text_model"),
        patch("plugin.chatbot.panel_factory.set_image_model"),
        patch("plugin.chatbot.panel_factory.set_control_enabled"),
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.chatbot.config_ui_helpers.get_current_endpoint", return_value=_ENDPOINT),
        patch("plugin.chatbot.config_ui_helpers.get_config", return_value=[]),
        patch("plugin.chatbot.config_ui_helpers.set_config", side_effect=set_config),
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value=_MODEL),
        patch("plugin.framework.client.model_fetcher.set_text_model"),
    ):
        bus.emit("config:changed")

    assert panel.refresh_count == 1
    assert "model_lru@%s" % _ENDPOINT in set_config_keys
