# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, cast

import uno
from com.sun.star.awt import XItemListener, XTextListener

if TYPE_CHECKING:
    from collections.abc import Callable
    from com.sun.star.awt import ItemEvent, TextEvent

from plugin.framework.errors import format_error_payload, UnoObjectError, ConfigValidationError
from plugin.framework.uno_context import get_desktop, get_extension_url, menu_icon_asset_url
from plugin.framework.i18n import _
from plugin.framework.config import get_config, get_current_endpoint, set_config, get_config_str, get_config_int
from plugin.framework.client.model_fetcher import get_text_model, get_stt_model, get_tts_model, set_text_model
from plugin.chatbot.settings_fields import populate_settings_control, read_settings_control
from plugin.framework.logging import init_logging
from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru
from plugin.chatbot.history_db import HAS_SQLITE
from plugin.scripting.venv_probe_ui import ScriptingVenvTestListener, VenvProbeProgressDialog

from plugin.framework.uno_listeners import BaseActionListener, BaseListener
from .dialogs import (
    TabListener, is_checkbox_control, get_checkbox_state,
    get_optional, set_control_enabled, set_control_text, get_control_text, translate_dialog,
    load_writeragent_dialog, msgbox,
)

log = logging.getLogger(__name__)

# PushButton ImageURL with ImagePosition LeftCenter places the icon directly on the button.
_PROVIDER_STARTER_ICONS = {
    "btn_openrouter": "openrouter",
    "btn_together": "together",
    "btn_hf": "huggingface",
    "btn_nvidia": "nvidia",
}
# com.sun.star.awt.ImagePosition.LeftCenter
_IMAGE_POSITION_LEFT_CENTER = 1


# Compact starter buttons: 16 on 1×; HiDPI keeps 48 (UnoControlButton does not
# scale ImageURL). Mid scales use 32. Do not use dialog getPosSize — AppFont
# before execute would pick tiny icons on HiDPI (see provider_logos.NOTICE).
_PROVIDER_ICON_PX = (16, 32, 48)
# menu_icon_dpi returns 16/26/32; map so ~2× still gets the 48 mark that looked good.
_MENU_PX_TO_PROVIDER = {16: 16, 26: 32, 32: 48}


def provider_icon_filename(stem: str, ctx: Any = None, px: int | None = None) -> str:
    """Asset basename under extension/assets/ (openrouter_16.png / _32 / _48).

    Picks a shipped size from VCL DPI so 1× buttons are not clipped by a 48px
    mark, while HiDPI keeps the large mark. Probe miss prefers HiDPI large.
    """
    import os

    from plugin.framework.menu_icon_dpi import resolve_menu_icon_pixel_size
    from plugin.framework.uno_context import menu_icon_filesystem_paths

    if px is None:
        menu_px = resolve_menu_icon_pixel_size(ctx)
        px = _MENU_PX_TO_PROVIDER.get(int(menu_px), 48)
    existing = []
    for cand in _PROVIDER_ICON_PX:
        name = "%s_%s.png" % (stem, cand)
        if any(os.path.isfile(path) for path in menu_icon_filesystem_paths(name)):
            existing.append(cand)
    if not existing:
        return "%s_48.png" % stem
    best = min(existing, key=lambda a: (abs(a - int(px)), -a))
    return "%s_%s.png" % (stem, best)


def apply_provider_button_icon(ctrl: Any, ctx: Any, stem: str) -> None:
    """Load the mark onto a PushButton control model aligned with LeftCenter."""
    filename = provider_icon_filename(stem, ctx=ctx)
    try:
        ext_url = get_extension_url(ctx)
        if not ext_url:
            return
        model = ctrl.getModel()
        model.ImageURL = menu_icon_asset_url(ext_url, filename)
        try:
            model.ImagePosition = _IMAGE_POSITION_LEFT_CENTER
        except Exception:
            pass
    except Exception:
        log.debug("Provider button icon %s failed", filename, exc_info=True)


def _load_selection_token_controls(extend_ctrl: Any, edit_extra_ctrl: Any) -> None:
    if extend_ctrl:
        set_control_text(extend_ctrl, str(get_config_int("extend_selection_max_tokens")))
    if edit_extra_ctrl:
        set_control_text(edit_extra_ctrl, str(get_config_int("edit_selection_max_new_tokens")))


def _save_selection_token_controls(extend_ctrl: Any, edit_extra_ctrl: Any) -> None:
    if extend_ctrl:
        set_config("extend_selection_max_tokens", get_control_text(extend_ctrl))
    if edit_extra_ctrl:
        set_config("edit_selection_max_new_tokens", get_control_text(edit_extra_ctrl))


# ── Generic Helpers ──────────────────────────────────────────────────

def input_box(ctx: Any, message: str, title: str = "", default: str = "", x: Any = None, y: Any = None) -> tuple[str, str]:
    """Shows input dialog (EditInputDialog.xdl). Returns (result_text, extra_prompt) if OK, else ("", "")."""
    init_logging(ctx)
    log.debug("input_box: opening Edit Input dialog")
    # Same loader as the other XDL dialogs: this ctx, then DialogProvider2
    # when DialogProvider cannot create the window. None is a failed load;
    # the loader already logged the provider errors.
    dlg = load_writeragent_dialog("EditInputDialog", ctx)
    if dlg is None:
        log.error("input_box: failed to create dialog")
        raise UnoObjectError("Failed to create dialog: EditInputDialog")

    need_dispose = True
    try:
        translate_dialog(dlg)

        dlg.getControl("label").getModel().Label = str(message)
        set_control_text(dlg.getControl("edit"), str(default))
        if title:
            dlg.getModel().Title = title

        prompt_ctrl = dlg.getControl("prompt_selector")
        current_prompt = get_config_str("additional_instructions")
        populate_combobox_with_lru(ctx, prompt_ctrl, current_prompt, "prompt_lru", "")

        model_selector = get_optional(dlg, "model_selector")
        if model_selector:
            current_endpoint = get_current_endpoint()
            current_model = get_text_model()
            # Fill from LRU plus provider defaults. A catalog HTTP on this
            # thread blocks LibreOffice for the full timeout on a slow or
            # dead endpoint, and a failure is not memoized.
            populate_combobox_with_lru(
                ctx, model_selector, current_model, "model_lru", current_endpoint,
                skip_remote_fetch=True,
            )

        extend_tokens_ctrl = get_optional(dlg, "extend_max_tokens")
        extra_tokens_ctrl = get_optional(dlg, "edit_extra_tokens")
        _load_selection_token_controls(extend_tokens_ctrl, extra_tokens_ctrl)

        dlg.getControl("edit").setFocus()
        dlg.getControl("edit").setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", 0, len(str(default))))

        if dlg.execute():
            ret_text = get_control_text(dlg.getControl("edit"))
            ret_prompt = prompt_ctrl.getText()
            if model_selector:
                chosen = model_selector.getText()
                if chosen:
                    set_text_model(chosen, update_lru=True)
            _save_selection_token_controls(extend_tokens_ctrl, extra_tokens_ctrl)
            return ret_text, ret_prompt
        # ESC/close: execute() returned false — skip dispose in finally (double dispose segfaults LO).
        need_dispose = False
        return "", ""
    except Exception as e:
        log.exception("input_box failed")
        raise UnoObjectError(f"Error in input_box: {e}") from e
    finally:
        if need_dispose:
            dlg.dispose()


class SettingsDialog:
    """Manages the lifecycle of the WriterAgent Settings dialog."""

    _ctx: Any
    _dlg: Any
    _scripting_venv_test_listener: Any
    _ppt_master_data_test_listener: Any
    _download_audio_listener: Any
    _copy_mcp_listener: Any
    _test_tunnel_listener: Any
    _grammar_recheck_listener: Any
    _mcp_tunnel_enabled_listener: Any
    _mcp_tunnel_provider_listener: Any
    _mcp_port_listener: Any
    _tts_listener: Any
    _tts_voice_listener: Any
    _tts_test_listener: Any
    _stt_listener: Any

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._dlg = None
        self._endpoint_listener: Any = None
        self._api_key_listener: Any = None
        self._scripting_venv_test_listener = None
        self._ppt_master_data_test_listener = None
        self._download_audio_listener = None
        self._copy_mcp_listener = None
        self._test_tunnel_listener = None
        self._grammar_recheck_listener = None
        self._mcp_tunnel_enabled_listener = None
        self._mcp_tunnel_provider_listener = None
        self._mcp_port_listener = None
        self._tts_listener = None
        self._tts_voice_listener = None
        self._tts_test_listener = None
        self._stt_listener = None

    def show(self) -> dict[str, Any]:
        """Execute the settings dialog and apply results."""
        from .settings_dialog import get_settings_field_specs, apply_settings_result

        log.debug("SettingsDialog.show entry")
        init_logging(self._ctx)

        try:
            self._create_dialog()
            if self._dlg is None:
                return {}

            set_active_settings_dialog(self._dlg)

            field_specs = get_settings_field_specs(self._ctx)
            current_endpoint = get_current_endpoint()

            self._setup_tabs()
            self._populate_fields(field_specs, current_endpoint)
            self._schedule_initial_models_fetch(current_endpoint)
            self._apply_sqlite_restrictions()
            
            translate_dialog(self._dlg)
            try:
                self._dlg.getModel().Title = _("Settings")
            except Exception:
                pass

            self._dlg.getControl("endpoint").setFocus()

            if self._dlg.execute():
                result = self._extract_results(field_specs)
                if result:
                    try:
                        apply_settings_result(self._ctx, result)
                        return result
                    except ConfigValidationError as ve:
                        msgbox(self._ctx, _("Invalid Setting"), str(ve))
                        return {}
            return {}
        except Exception as e:
            log.exception("Failed to open Settings")
            msgbox(self._ctx, _("Error"), _("Failed to open Settings: {0}").format(e))
            return format_error_payload(e)
        finally:
            self._cleanup()

    def _create_dialog(self) -> None:
        # Loader returns None after DialogProvider and DialogProvider2 both fail.
        # show() only message-boxes exceptions, so a silent None would close with no dialog.
        self._dlg = load_writeragent_dialog("SettingsDialog", self._ctx)
        if self._dlg is None:
            raise UnoObjectError("Failed to create dialog: SettingsDialog")

    def _setup_tabs(self) -> None:
        assert self._dlg is not None
        self._dlg.getControl("btn_tab_chat").addActionListener(TabListener(self._dlg, 1))
        self._dlg.getControl("btn_tab_image").addActionListener(TabListener(self._dlg, 2))
        
        edit_config_btn = get_optional(self._dlg, "btn_edit_config_json")
        if edit_config_btn:
            edit_config_btn.addActionListener(EditConfigListener(self._ctx))

        starters = [
            ("btn_openrouter", "https://openrouter.ai/api", "https://openrouter.ai/keys"),
            ("btn_together", "https://api.together.xyz", "https://api.together.ai/settings/api-keys"),
            ("btn_hf", "https://router.huggingface.co/v1", "https://huggingface.co/settings/tokens"),
            ("btn_nvidia", "https://integrate.api.nvidia.com/v1", "https://build.nvidia.com/settings/api-keys"),
        ]
        for btn_id, ep_url, signup_url in starters:
            btn = get_optional(self._dlg, btn_id)
            if not btn:
                continue
            starter = ProviderStarterListener(self._ctx, self._dlg, ep_url, signup_url)
            btn.addActionListener(starter)
            stem = _PROVIDER_STARTER_ICONS.get(btn_id)
            if stem:
                apply_provider_button_icon(btn, self._ctx, stem)

        test_conn_btn = get_optional(self._dlg, "btn_test_conn")
        if test_conn_btn:
            test_conn_btn.addActionListener(
                TestConnectionListener(
                    self._ctx, self._dlg, catalog_recheck=self._force_catalog_recheck, api_key_override=self._endpoint_api_key_override
                )
            )

        self._setup_module_tabs()
        test_venv_btn = get_optional(self._dlg, "scripting__test_venv")
        if test_venv_btn:
            self._scripting_venv_test_listener = ScriptingVenvTestListener(self._ctx, self._dlg)
            test_venv_btn.addActionListener(self._scripting_venv_test_listener)

        test_ppt_btn = get_optional(self._dlg, "scripting__test_ppt_master_data")
        if test_ppt_btn:
            self._ppt_master_data_test_listener = PptMasterDataTestListener(self._ctx, self._dlg)
            test_ppt_btn.addActionListener(self._ppt_master_data_test_listener)

        download_audio_btn = get_optional(self._dlg, "scripting__download_audio_binaries")
        if download_audio_btn:
            self._download_audio_listener = DownloadAudioListener(self._ctx, self._dlg)
            download_audio_btn.addActionListener(self._download_audio_listener)

        copy_mcp_btn = get_optional(self._dlg, "mcp__copy_config")
        if copy_mcp_btn:
            self._copy_mcp_listener = CopyMcpConfigListener(self._ctx, self._dlg)
            copy_mcp_btn.addActionListener(self._copy_mcp_listener)

        test_tunnel_btn = get_optional(self._dlg, "mcp__test_tunnel")
        if test_tunnel_btn:
            self._test_tunnel_listener = TestTunnelListener(self._ctx, self._dlg)
            test_tunnel_btn.addActionListener(self._test_tunnel_listener)

        recheck_btn = get_optional(self._dlg, "doc__grammar_proofreader_recheck")
        if recheck_btn:
            self._grammar_recheck_listener = RecheckGrammarListener(self._ctx)
            recheck_btn.addActionListener(self._grammar_recheck_listener)

        port_ctrl = get_optional(self._dlg, "mcp__mcp_port")
        if port_ctrl and hasattr(port_ctrl, "addTextListener"):
            self._mcp_port_listener = McpPortTextListener(self._dlg)
            port_ctrl.addTextListener(self._mcp_port_listener)

        tunnel_enabled_ctrl = get_optional(self._dlg, "mcp__tunnel_enabled")
        if tunnel_enabled_ctrl and hasattr(tunnel_enabled_ctrl, "addItemListener"):
            self._mcp_tunnel_enabled_listener = McpTunnelEnabledListener(self._dlg)
            tunnel_enabled_ctrl.addItemListener(self._mcp_tunnel_enabled_listener)

        provider_ctrl = get_optional(self._dlg, "mcp__tunnel_provider")
        if provider_ctrl:
            self._mcp_tunnel_provider_listener = McpTunnelProviderListener(self._dlg)
            if hasattr(provider_ctrl, "addItemListener"):
                provider_ctrl.addItemListener(self._mcp_tunnel_provider_listener)
            if hasattr(provider_ctrl, "addTextListener"):
                provider_ctrl.addTextListener(self._mcp_tunnel_provider_listener)

    def _setup_module_tabs(self) -> None:
        try:
            # Register module tabs in the Settings dialog
            setup_module_tabs(self._dlg)
        except Exception:
            # setup_module_tabs already logs its own failures. This used to
            # swallow that and leave the dialog with dead module tabs.
            log.exception("Failed to set up module tabs")

    def _api_key_from_field_specs(self, field_specs: list[dict[str, Any]]) -> str:
        for field in field_specs:
            if field.get("name") == "api_key":
                return str(field.get("value") or "")
        return ""

    def _populate_fields(self, field_specs: list[dict[str, Any]], current_endpoint: str) -> None:
        assert self._dlg is not None
        from plugin.chatbot.config_ui_helpers import (
            populate_combobox_with_lru, populate_image_model_selector, populate_endpoint_selector
        )

        api_key_val = self._api_key_from_field_specs(field_specs)

        for field in field_specs:
            # get_optional returns None for a name missing from the XDL
            # (and still raises if the dialog is disposed). getControl
            # raises on a missing name and aborts show() before execute().
            ctrl = get_optional(self._dlg, field["name"])
            if ctrl is None:
                log.warning("Settings dialog missing control %r", field["name"])
                continue

            name = field["name"]
            val = field["value"]

            if name == "text_model":
                # LRU plus defaults only. This fill runs before execute();
                # a catalog fetch here freezes LibreOffice for the timeout.
                # _schedule_initial_models_fetch loads it on the debounced worker.
                populate_combobox_with_lru(
                    self._ctx, ctrl, val, "model_lru", current_endpoint,
                    api_key_override=api_key_val, skip_remote_fetch=True,
                )
            elif name == "image_model":
                populate_image_model_selector(
                    self._ctx, ctrl, override_endpoint=current_endpoint,
                    api_key_override=api_key_val, skip_remote_fetch=True,
                )
            elif name in ("audio__stt_model", "stt_model"):
                populate_combobox_with_lru(
                    self._ctx, ctrl, val, "audio_model_lru", current_endpoint,
                    api_key_override=api_key_val, skip_remote_fetch=True,
                )
            elif name in ("audio__tts_model", "tts_model"):
                populate_combobox_with_lru(
                    self._ctx, ctrl, val, "tts_model_lru", current_endpoint,
                    api_key_override=api_key_val, skip_remote_fetch=True,
                )
            elif name == "additional_instructions":
                populate_combobox_with_lru(self._ctx, ctrl, val, "prompt_lru", "")
            elif name == "endpoint":
                populate_endpoint_selector(self._ctx, ctrl, val)
                self._setup_endpoint_listener(ctrl)
            elif name == "image_base_size":
                populate_combobox_with_lru(self._ctx, ctrl, val, "image_base_size_lru", "")
            else:
                self._populate_generic_field(ctrl, field)

        # Populate non-persisted client config snippet
        sync_mcp_config_snippet(self._dlg)
        self._setup_tts_listeners()
        self._setup_stt_listeners()

    def _setup_tts_listeners(self) -> None:
        test_btn = get_optional(self._dlg, "audio__test_voice")
        if test_btn and hasattr(test_btn, "addActionListener"):
            self._tts_test_listener = TtsTestVoiceListener(self._ctx, self._dlg)
            test_btn.addActionListener(self._tts_test_listener)

        prov_ctrl = get_optional(self._dlg, "audio__tts_provider")
        voice_ctrl = get_optional(self._dlg, "audio__tts_voice")
        model_ctrl = get_optional(self._dlg, "audio__tts_model") or get_optional(self._dlg, "tts_model")

        if not (prov_ctrl or voice_ctrl or model_ctrl):
            return

        self._tts_listener = TtsSettingsListener(self._dlg, self._ctx)

        if prov_ctrl and hasattr(prov_ctrl, "addItemListener"):
            prov_ctrl.addItemListener(self._tts_listener)
            if hasattr(prov_ctrl, "addTextListener"):
                prov_ctrl.addTextListener(self._tts_listener)

        if model_ctrl and hasattr(model_ctrl, "addItemListener"):
            model_ctrl.addItemListener(self._tts_listener)
            if hasattr(model_ctrl, "addTextListener"):
                model_ctrl.addTextListener(self._tts_listener)

        if voice_ctrl and hasattr(voice_ctrl, "addItemListener"):
            self._tts_voice_listener = TtsVoiceListener(self._dlg, self._tts_listener)
            voice_ctrl.addItemListener(self._tts_voice_listener)
            if hasattr(voice_ctrl, "addTextListener"):
                voice_ctrl.addTextListener(self._tts_voice_listener)

        self._tts_listener.sync_ui()

    def _setup_stt_listeners(self) -> None:
        prov_ctrl = get_optional(self._dlg, "audio__stt_provider")
        if prov_ctrl is None:
            return
        self._stt_listener = SttSettingsListener(self._dlg)
        if hasattr(prov_ctrl, "addItemListener"):
            prov_ctrl.addItemListener(self._stt_listener)
        if hasattr(prov_ctrl, "addTextListener"):
            prov_ctrl.addTextListener(self._stt_listener)
        self._stt_listener.sync_ui()

    def _endpoint_api_key_override(self) -> str | None:
        """The key the model fetch would send for the current URL, or None."""
        listener = self._endpoint_listener
        if listener is None:
            return None
        return listener._api_key_override()

    def _force_catalog_recheck(self) -> None:
        """Test Connection: refetch catalogs even when the process memo is warm."""
        listener = self._endpoint_listener
        if listener is not None:
            listener.force_catalog_refresh()

    def _schedule_initial_models_fetch(self, endpoint: str) -> None:
        """Every provider: combos are already LRU. Fetch the catalog off the UI thread.

        Every provider fetches here, off the UI thread. Returning unless
        the provider was OpenRouter or Together left Ollama, Groq, and custom
        URLs with only the open-time fill, and that load blocked execute().
        """
        from plugin.framework.config import get_api_key_for_endpoint
        from plugin.framework.client.auth import provider_requires_api_key
        from plugin.framework.client.provider_detection import get_provider_from_endpoint

        listener = self._endpoint_listener
        if not listener or not endpoint:
            return
        provider = get_provider_from_endpoint(endpoint)
        # A key-gated host with an empty key cannot list models. Local and
        # custom endpoints do not use that gate.
        if provider and provider_requires_api_key(provider):
            if not str(get_api_key_for_endpoint(endpoint) or "").strip():
                return
        listener._schedule_debounced_models_fetch()

    def _populate_generic_field(self, ctrl: Any, field: dict[str, Any]) -> None:
        populate_settings_control(ctrl, field)

    def _setup_endpoint_listener(self, ctrl: Any) -> None:
        if hasattr(ctrl, "addItemListener"):
            self._endpoint_listener = EndpointCombinedListener(self._dlg, self._ctx, ctrl)
            ctrl.addItemListener(self._endpoint_listener)
            if hasattr(ctrl, "addTextListener"):
                ctrl.addTextListener(self._endpoint_listener)

            ak_ctrl = get_optional(self._dlg, "api_key")
            if ak_ctrl and hasattr(ak_ctrl, "addTextListener"):
                self._api_key_listener = ApiKeyTextListener(self._endpoint_listener)
                ak_ctrl.addTextListener(self._api_key_listener)

    def _apply_sqlite_restrictions(self) -> None:
        if not HAS_SQLITE:
            for name in (
                "chatbot__web_cache_max_mb",
                "chatbot__web_cache_validity_days",
                "chatbot__web_research_cache_enabled",
            ):
                ctrl = get_optional(self._dlg, name)
                if ctrl:
                    set_control_enabled(ctrl, False)

    def _extract_results(self, field_specs: list[dict[str, Any]]) -> dict[str, Any]:
        assert self._dlg is not None
        # Mixed checkbox bools and text strings; without this mypy infers dict[str, str]
        # from the empty-control "" default once SettingsDialog has class-body annotations.
        result: dict[str, Any] = {}
        for field in field_specs:
            name = field["name"]
            # getControl raises for an unknown id. None used to be the only
            # skip, so one missing name aborted OK the same way it aborted
            # open. get_optional skips the name; a disposed dialog still raises.
            ctrl = get_optional(self._dlg, name)
            if ctrl is None:
                continue

            try:
                value = read_settings_control(ctrl, field)
            except Exception:
                log.exception("Failed to extract field %s", name)
                continue
            if value is None:
                continue
            result[name] = value
        return result

    def _cleanup(self) -> None:
        if self._api_key_listener:
            ak = get_optional(self._dlg, "api_key")
            if ak and hasattr(ak, "removeTextListener"):
                ak.removeTextListener(self._api_key_listener)
        if self._endpoint_listener:
            self._endpoint_listener.close()
        if self._scripting_venv_test_listener and self._dlg is not None:
            test_venv_btn = get_optional(self._dlg, "scripting__test_venv")
            if test_venv_btn and hasattr(test_venv_btn, "removeActionListener"):
                try:
                    test_venv_btn.removeActionListener(self._scripting_venv_test_listener)
                except Exception:
                    pass
            self._scripting_venv_test_listener = None
        if self._ppt_master_data_test_listener and self._dlg is not None:
            test_ppt_btn = get_optional(self._dlg, "scripting__test_ppt_master_data")
            if test_ppt_btn and hasattr(test_ppt_btn, "removeActionListener"):
                try:
                    test_ppt_btn.removeActionListener(self._ppt_master_data_test_listener)
                except Exception:
                    pass
            self._ppt_master_data_test_listener = None
        if self._download_audio_listener and self._dlg is not None:
            download_audio_btn = get_optional(self._dlg, "scripting__download_audio_binaries")
            if download_audio_btn and hasattr(download_audio_btn, "removeActionListener"):
                try:
                    download_audio_btn.removeActionListener(self._download_audio_listener)
                except Exception:
                    pass
            self._download_audio_listener = None
        if self._copy_mcp_listener and self._dlg is not None:
            copy_mcp_btn = get_optional(self._dlg, "mcp__copy_config")
            if copy_mcp_btn and hasattr(copy_mcp_btn, "removeActionListener"):
                try:
                    copy_mcp_btn.removeActionListener(self._copy_mcp_listener)
                except Exception:
                    pass
            self._copy_mcp_listener = None
        if self._test_tunnel_listener and self._dlg is not None:
            test_tunnel_btn = get_optional(self._dlg, "mcp__test_tunnel")
            if test_tunnel_btn and hasattr(test_tunnel_btn, "removeActionListener"):
                try:
                    test_tunnel_btn.removeActionListener(self._test_tunnel_listener)
                except Exception:
                    pass
            self._test_tunnel_listener = None
        if self._grammar_recheck_listener and self._dlg is not None:
            recheck_btn = get_optional(self._dlg, "doc__grammar_proofreader_recheck")
            if recheck_btn and hasattr(recheck_btn, "removeActionListener"):
                try:
                    recheck_btn.removeActionListener(self._grammar_recheck_listener)
                except Exception:
                    pass
            self._grammar_recheck_listener = None
        if self._mcp_tunnel_enabled_listener and self._dlg is not None:
            tunnel_enabled_ctrl = get_optional(self._dlg, "mcp__tunnel_enabled")
            if tunnel_enabled_ctrl and hasattr(tunnel_enabled_ctrl, "removeItemListener"):
                try:
                    tunnel_enabled_ctrl.removeItemListener(self._mcp_tunnel_enabled_listener)
                except Exception:
                    pass
            self._mcp_tunnel_enabled_listener = None
        if self._mcp_tunnel_provider_listener and self._dlg is not None:
            provider_ctrl = get_optional(self._dlg, "mcp__tunnel_provider")
            if provider_ctrl:
                if hasattr(provider_ctrl, "removeItemListener"):
                    try:
                        provider_ctrl.removeItemListener(self._mcp_tunnel_provider_listener)
                    except Exception:
                        pass
                if hasattr(provider_ctrl, "removeTextListener"):
                    try:
                        provider_ctrl.removeTextListener(self._mcp_tunnel_provider_listener)
                    except Exception:
                        pass
            self._mcp_tunnel_provider_listener = None
        if self._mcp_port_listener and self._dlg is not None:
            port_ctrl = get_optional(self._dlg, "mcp__mcp_port")
            if port_ctrl and hasattr(port_ctrl, "removeTextListener"):
                try:
                    port_ctrl.removeTextListener(self._mcp_port_listener)
                except Exception:
                    pass
            self._mcp_port_listener = None
        if self._tts_listener and self._dlg is not None:
            prov_ctrl = get_optional(self._dlg, "audio__tts_provider")
            if prov_ctrl and hasattr(prov_ctrl, "removeItemListener"):
                try:
                    prov_ctrl.removeItemListener(self._tts_listener)
                except Exception:
                    pass
            if prov_ctrl and hasattr(prov_ctrl, "removeTextListener"):
                try:
                    prov_ctrl.removeTextListener(self._tts_listener)
                except Exception:
                    pass
            model_ctrl = get_optional(self._dlg, "audio__tts_model") or get_optional(self._dlg, "tts_model")
            if model_ctrl and hasattr(model_ctrl, "removeItemListener"):
                try:
                    model_ctrl.removeItemListener(self._tts_listener)
                except Exception:
                    pass
            if model_ctrl and hasattr(model_ctrl, "removeTextListener"):
                try:
                    model_ctrl.removeTextListener(self._tts_listener)
                except Exception:
                    pass
            self._tts_listener = None
        if self._tts_voice_listener and self._dlg is not None:
            voice_ctrl = get_optional(self._dlg, "audio__tts_voice")
            if voice_ctrl and hasattr(voice_ctrl, "removeItemListener"):
                try:
                    voice_ctrl.removeItemListener(self._tts_voice_listener)
                except Exception:
                    pass
            if voice_ctrl and hasattr(voice_ctrl, "removeTextListener"):
                try:
                    voice_ctrl.removeTextListener(self._tts_voice_listener)
                except Exception:
                    pass
            self._tts_voice_listener = None
        if self._tts_test_listener and self._dlg is not None:
            test_btn = get_optional(self._dlg, "audio__test_voice")
            if test_btn and hasattr(test_btn, "removeActionListener"):
                try:
                    test_btn.removeActionListener(self._tts_test_listener)
                except Exception:
                    pass
            self._tts_test_listener = None
        if self._stt_listener and self._dlg is not None:
            prov_ctrl = get_optional(self._dlg, "audio__stt_provider")
            if prov_ctrl and hasattr(prov_ctrl, "removeItemListener"):
                try:
                    prov_ctrl.removeItemListener(self._stt_listener)
                except Exception:
                    pass
            if prov_ctrl and hasattr(prov_ctrl, "removeTextListener"):
                try:
                    prov_ctrl.removeTextListener(self._stt_listener)
                except Exception:
                    pass
            self._stt_listener = None
        clear_active_settings_dialog(self._dlg)
        if self._dlg:
            self._dlg.dispose()


def settings_box(ctx: Any, **kwargs: Any) -> Any:
    """Entry point for settings dialog."""
    return SettingsDialog(ctx).show()


# ── Listeners ────────────────────────────────────────────────────────

def open_system_url(ctx: Any, url_str: str) -> None:
    """Open URL in default browser via UNO SystemShellExecute."""
    if not url_str:
        return
    try:
        smgr = ctx.getServiceManager()
        shell = smgr.createInstanceWithContext("com.sun.star.system.SystemShellExecute", ctx)
        shell.execute(url_str, "", 0)
    except Exception as e:
        log.warning("Failed to open URL %s: %s", url_str, e)


class EditConfigListener(BaseActionListener):
    _ctx: Any

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
    def on_action_performed(self, rEvent: Any) -> None:
        from .external_editor import open_writeragent_json_in_editor
        open_writeragent_json_in_editor(self._ctx)


class RecheckGrammarListener(BaseActionListener):
    """Doc tab: dump this document's grammar L2 plus all of L1, then PROOFREAD_AGAIN."""

    _ctx: Any

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    def on_action_performed(self, rEvent: Any) -> None:
        del rEvent
        from plugin.writer.locale.grammar_proofread_cache import recheck_active_document_grammar

        recheck_active_document_grammar(self._ctx)


class ProviderStarterListener(BaseActionListener):
    """When a provider starter button is clicked, select its endpoint, sync key, and open signup page."""

    _ctx: Any
    _dlg: Any
    _endpoint_url: str
    _signup_url: str

    def __init__(self, ctx: Any, dlg: Any, endpoint_url: str, signup_url: str) -> None:
        self._ctx = ctx
        self._dlg = dlg
        self._endpoint_url = endpoint_url
        self._signup_url = signup_url

    def on_action_performed(self, rEvent: Any) -> None:
        endpoint_ctrl = get_optional(self._dlg, "endpoint")
        if endpoint_ctrl:
            set_control_text(endpoint_ctrl, self._endpoint_url)
            ak_ctrl = get_optional(self._dlg, "api_key")
            if ak_ctrl:
                from plugin.framework.config import get_api_key_for_endpoint
                set_control_text(ak_ctrl, get_api_key_for_endpoint(self._endpoint_url))
                if hasattr(ak_ctrl, "setFocus"):
                    ak_ctrl.setFocus()
        if self._signup_url:
            open_system_url(self._ctx, self._signup_url)


class TestConnectionListener(BaseActionListener):
    _ctx: Any
    _dlg: Any
    _catalog_recheck: Any
    _api_key_override: Any

    def __init__(self, ctx: Any, dlg: Any, catalog_recheck: Any = None, api_key_override: Any = None) -> None:
        self._ctx = ctx
        self._dlg = dlg
        # Returns the key the endpoint listener would send for the current URL.
        self._api_key_override = api_key_override
        # Test Connection is the explicit catalog recheck. Opening Settings,
        # typing, and OK do not refetch a warm OpenRouter/Together memo.
        self._catalog_recheck = catalog_recheck

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text
        from plugin.chatbot.quick_setup import check_endpoint_connection
        from plugin.framework.worker_pool import run_in_background
        from plugin.framework.queue_executor import post_to_main_thread

        btn_test = get_optional(self._dlg, "btn_test_conn")
        lbl_status = get_optional(self._dlg, "lbl_test_status")
        if btn_test:
            set_control_enabled(btn_test, False)
        if lbl_status:
            set_control_text(lbl_status, _("Testing connection..."))
        # UI thread: reads the endpoint controls, then the catalog GET runs
        # on the settings worker. Do not wait for the connection probe.
        if self._catalog_recheck is not None:
            self._catalog_recheck()

        endpoint_ctrl = get_optional(self._dlg, "endpoint")
        endpoint_text = str(get_control_text(endpoint_ctrl)) if endpoint_ctrl else ""
        endpoint = endpoint_from_selector_text(endpoint_text)

        api_key_ctrl = get_optional(self._dlg, "api_key")
        # Same key decision as the model fetch (effective_api_key via the
        # endpoint listener), so Test Connection never sends a stale key.
        override = self._api_key_override() if self._api_key_override is not None else None
        if override is not None:
            api_key = override
        elif api_key_ctrl:
            from plugin.chatbot.settings_dialog import effective_api_key
            from plugin.framework.config import get_api_key_for_endpoint

            saved_endpoint = get_current_endpoint()
            api_key = effective_api_key(
                str(get_control_text(api_key_ctrl)),
                saved_endpoint,
                endpoint,
                get_api_key_for_endpoint(saved_endpoint),
                get_api_key_for_endpoint(endpoint),
            )
        else:
            api_key = ""

        def _worker() -> None:
            msg = check_endpoint_connection(endpoint, api_key)[1]

            def _apply() -> None:
                if btn_test:
                    set_control_enabled(btn_test, True)
                if lbl_status:
                    set_control_text(lbl_status, _(msg))

            post_to_main_thread(_apply)

        run_in_background(_worker, name="settings-test-conn")


def _dialog_parent_for_child(ctx: Any, parent_dlg: Any) -> Any:  # pyright: ignore[reportUnusedFunction]  # settings peer parent helper; used by tests
    """Resolve a parent window for a child modal opened above an executing dialog."""
    if parent_dlg is not None:
        try:
            peer = parent_dlg.getPeer()
            if peer is not None:
                return peer
        except Exception:
            log.debug("parent_dlg.getPeer failed for child modal", exc_info=True)
    try:
        desktop = get_desktop(ctx)
        frame = desktop.getCurrentFrame() if desktop else None
        if frame is not None:
            return frame.getContainerWindow()
    except Exception:
        log.debug("getCurrentFrame parent fallback failed for child modal", exc_info=True)
    return None


class PptMasterDataTestListener(BaseActionListener):
    """Settings → Python: verify ppt-master skill tree at the path in the text field (saved or not)."""

    _ctx: Any
    _dlg: Any

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.ppt_master.paths import probe_data_path_with_progress

        path_ctrl = get_optional(self._dlg, "scripting__ppt_master_data_path")
        raw = get_control_text(path_ctrl) if path_ctrl else ""

        def probe(on_display: Callable[[str], None], on_status: Callable[[str], None] | None) -> Any:
            return probe_data_path_with_progress(raw, on_display, on_status=on_status)

        VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg).run_modal_probe(probe)


def _dialog_endpoint_url(dlg: Any) -> str | None:
    """Endpoint URL from the Settings combo, or None when that control is absent.

    None lets voice lookup use the saved endpoint. A present control is what
    the Speech model list was just filled from, including an unsaved preset.
    """
    ctrl = get_optional(dlg, "endpoint")
    if ctrl is None:
        return None
    from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text

    return endpoint_from_selector_text(get_control_text(ctrl))


def _dialog_api_key(dlg: Any) -> str | None:
    """API key typed in Settings, or None when the field is not on this dialog."""
    ctrl = get_optional(dlg, "api_key")
    if ctrl is None:
        return None
    return get_control_text(ctrl)


def _apply_stt_model_visibility(dlg: Any, provider_text: str) -> None:
    """Enable Audio Model for endpoint STT and Local Model for faster-whisper.

    Do not call ``XWindow.setVisible`` on these controls. Settings is one
    dialog with steps (General is step 1, Image is step 2, Speech is later).
    Audio Model is on the Speech step at the same Y as API Key. Each
    ``SettingsDialog.show`` builds a new dialog: ``createDialog`` already has
    a peer, then this sync runs, then ``execute()`` shows General. The first
    show never goes through ``TabListener``. ``setVisible(True)`` in that
    window paints the control on the current step and leaves its own Step
    unchanged, so every new dialog stacks "Audio Model:" on "API Key:".
    Closing and reopening builds another dialog and does it again. Assigning
    Step to 1 while already on General does not re-filter. Tab buttons only
    assign Step; they do not refresh STT visibility. Image then General does
    re-filter and hides the stray control. ``setEnable`` does not paint
    across steps; TTS Model already uses it for the same reason.
    """
    from plugin.audio.stt_service import stt_controls_enabled

    endpoint_on, local_on = stt_controls_enabled(provider_text)
    groups = (
        (endpoint_on, ("audio__stt_model", "stt_model"), ("label_audio__stt_model", "label_stt_model")),
        (local_on, ("audio__stt_local_model",), ("label_audio__stt_local_model",)),
    )
    for enabled, control_names, label_names in groups:
        for name in control_names + label_names:
            ctrl = get_optional(dlg, name)
            if ctrl is None:
                continue
            set_control_enabled(ctrl, enabled)


class SttSettingsListener(BaseListener, XItemListener, XTextListener):
    """Enables the endpoint STT model or the local Whisper size, not both."""

    _dlg: Any

    def __init__(self, dialog: Any) -> None:
        self._dlg = dialog

    def sync_ui(self) -> None:
        if not self._dlg:
            return
        try:
            prov_ctrl = get_optional(self._dlg, "audio__stt_provider")
            raw = prov_ctrl.getText() if prov_ctrl is not None and hasattr(prov_ctrl, "getText") else ""
            _apply_stt_model_visibility(self._dlg, raw)
        except Exception:
            log.exception("Error syncing STT provider controls")

    def itemStateChanged(self, rEvent: ItemEvent) -> None:
        self.sync_ui()

    def textChanged(self, rEvent: TextEvent) -> None:
        self.sync_ui()


class TtsSettingsListener(BaseListener, XItemListener, XTextListener):
    """Synchronizes TTS voice choices and model enablement when provider/model changes."""

    _dlg: Any
    _ctx: Any
    _syncing: bool

    def __init__(self, dialog: Any, ctx: Any) -> None:
        self._dlg = dialog
        self._ctx = ctx
        self._syncing = False

    def sync_ui(self) -> None:
        if self._syncing or not self._dlg:
            return
        self._syncing = True
        try:
            from plugin.audio.tts_voices import (
                _preferred_harvested_voice,
                clean_provider_name,
                clean_voice_name,
                get_default_voice_for_locale,
                get_voice_family,
                voice_choice_to_id,
                voice_options_for_provider,
            )
            from plugin.audio.tts_voices import get_config

            prov_ctrl = get_optional(self._dlg, "audio__tts_provider")
            model_ctrl = get_optional(self._dlg, "audio__tts_model") or get_optional(self._dlg, "tts_model")
            voice_ctrl = get_optional(self._dlg, "audio__tts_voice")

            raw_prov = prov_ctrl.getText() if prov_ctrl and hasattr(prov_ctrl, "getText") else ""
            provider = clean_provider_name(raw_prov)
            raw_model = model_ctrl.getText() if model_ctrl and hasattr(model_ctrl, "getText") else ""
            endpoint = _dialog_endpoint_url(self._dlg)
            api_key = _dialog_api_key(self._dlg)

            if model_ctrl:
                set_control_enabled(model_ctrl, provider == "endpoint")

            if voice_ctrl and hasattr(voice_ctrl, "getModel"):
                catalog = voice_options_for_provider(
                    provider, raw_model, endpoint=endpoint, api_key=api_key,
                )
                labels = tuple(opt["label"] for opt in catalog)

                model = voice_ctrl.getModel()
                if hasattr(model, "StringItemList"):
                    current_items = getattr(model, "StringItemList", ())
                    # UNO returns a tuple. A missing list is not one — assigning
                    # is what fills the dropdown. Do not iterate arbitrary objects.
                    if not isinstance(current_items, (tuple, list)) or tuple(current_items) != labels:
                        model.StringItemList = labels

                # Empty catalog: OpenRouter or Together speech model with no voice
                # list yet. Leave the combo editable. Do not fill it with alloy.
                if not catalog:
                    return

                current_text = voice_ctrl.getText() if hasattr(voice_ctrl, "getText") else ""
                # Display text is the parenthetical, not the id. Match this list.
                current_id = voice_choice_to_id(current_text, catalog)
                # Read the stored id so an empty combo can show it. Do not write
                # it back: opening Settings or switching provider used to call
                # set_scoped_tts_voice here, so Cancel could not undo the change
                # and a stale id was "repaired" on disk just by showing the
                # fallback. apply_settings_result persists the combo on OK.
                family = get_voice_family(provider, raw_model, endpoint)
                stored = clean_voice_name(str(get_config(f"audio.tts_voice_{family}") or ""))
                by_value = {opt["value"]: opt["label"] for opt in catalog}
                if current_id in by_value:
                    chosen = current_id
                elif not current_id and stored in by_value:
                    chosen = stored
                else:
                    # Visible or saved voice belongs to another model (often alloy).
                    # Remote lists share the speak fallback. Kokoro and Piper use
                    # the locale default when that id is in the catalog.
                    if provider == "endpoint" and family not in ("kokoro", "piper"):
                        chosen = _preferred_harvested_voice(
                            raw_model, [opt["value"] for opt in catalog],
                        )
                    else:
                        # Locale default (Piper female / Kokoro map), not merely
                        # whichever row the catalog listed first.
                        locale_default = get_default_voice_for_locale(family)
                        chosen = locale_default if locale_default in by_value else catalog[0]["value"]

                target_label = by_value.get(chosen, "")
                # Promote a raw voice id (or a previous family's label) to the catalog label.
                if target_label and current_text != target_label:
                    voice_ctrl.setText(target_label)
        except Exception:
            log.exception("Error syncing TTS UI settings")
        finally:
            self._syncing = False

    def itemStateChanged(self, rEvent: ItemEvent) -> None:
        self.sync_ui()

    def textChanged(self, rEvent: TextEvent) -> None:
        self.sync_ui()


class TtsVoiceListener(BaseListener, XItemListener, XTextListener):
    """Voice combo listener. The selection is stored only when Settings OK runs.

    The combo shows the selection; ``sync_ui`` updates it when the
    provider or model changes; ``apply_settings_result`` writes it.
    Saving ``audio.tts_voice*`` on every pick leaves the new voice on disk
    after Cancel.
    """

    _dlg: Any
    _tts_listener: TtsSettingsListener

    def __init__(self, dialog: Any, tts_listener: TtsSettingsListener) -> None:
        self._dlg = dialog
        self._tts_listener = tts_listener

    def itemStateChanged(self, rEvent: ItemEvent) -> None:
        self._on_change()

    def textChanged(self, rEvent: TextEvent) -> None:
        self._on_change()

    def _on_change(self) -> None:
        # Combo already shows the pick. Do not write config; OK does.
        return


class _TtsTestProgressBox:
    """Modeless Speech status for on-demand Piper/Kokoro downloads during Test voice.

    Native MessageBox.execute() cannot be closed from a download-completion
    callback (main thread is inside execute). This box uses setVisible and is
    dismissed via clear() on the UNO thread, or OK if the user dismisses early.
    """

    _ctx: Any
    _dlg: Any
    _closed: bool

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._dlg = None
        self._closed = False

    def show_or_update(self, message: str) -> None:
        if self._closed:
            return
        try:
            if self._dlg is None:
                self._open(message)
            else:
                msg_ctrl = self._dlg.getControl("Msg")
                if msg_ctrl is not None:
                    msg_ctrl.getModel().Label = message
        except Exception:
            log.debug("TTS test progress update failed", exc_info=True)

    def _open(self, message: str) -> None:
        from plugin.framework.uno_listeners import BaseActionListener as _BAL

        dlg = load_writeragent_dialog("MsgBoxWithCopyDialog", self._ctx)
        if dlg is None:
            # Last resort: blocking box (cannot auto-close). Prefer logging only.
            log.info("TTS test progress (no dialog): %s", message)
            return
        try:
            dlg.getModel().Title = _("Speech")
        except Exception:
            log.debug("TTS test progress title failed", exc_info=True)
        msg_ctrl = dlg.getControl("Msg")
        if msg_ctrl is not None:
            msg_ctrl.getModel().Label = message
        copy_btn = dlg.getControl("CopyBtn")
        if copy_btn is not None:
            try:
                if hasattr(copy_btn, "setVisible"):
                    copy_btn.setVisible(False)
                else:
                    copy_btn.getModel().Visible = False
            except Exception:
                log.debug("TTS test progress hide Copy failed", exc_info=True)

        owner = self

        class _OkListener(_BAL):
            def on_action_performed(self, rEvent: Any) -> None:
                del rEvent
                owner.close()

        ok_btn = dlg.getControl("OKBtn")
        if ok_btn is not None:
            ok_btn.addActionListener(_OkListener())
        self._dlg = dlg
        dlg.setVisible(True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        try:
            dlg.setVisible(False)
        except Exception:
            log.debug("TTS test progress hide failed", exc_info=True)
        try:
            dlg.dispose()
        except Exception:
            log.debug("TTS test progress dispose failed", exc_info=True)


class _TtsTestStatusSink:
    """Worker-facing status sink for Test voice (progress / clear / sticky errors)."""

    _listener: TtsTestVoiceListener

    def __init__(self, listener: TtsTestVoiceListener) -> None:
        self._listener = listener

    def progress(self, message: str) -> None:
        log.info("TTS test progress: %s", message)
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(self._listener._show_progress, message)

    def clear(self) -> None:
        log.info("TTS test progress: clear")
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(self._listener._close_progress)

    def __call__(self, message: str) -> None:
        # Fallback / HTTP body: close any progress box, then blocking MessageBox.
        log.info("TTS test: %s", message)
        from plugin.framework.queue_executor import post_to_main_thread

        def _show() -> None:
            self._listener._close_progress()
            self._listener._status(message)

        post_to_main_thread(_show)


class TtsTestVoiceListener(BaseActionListener):
    """Settings → Speech: speak a UI-locale sample with the controls on screen.

    OK has not necessarily saved yet. The checkbox, provider, model, voice,
    and speed in this dialog are what the user is trying to hear.
    """

    _ctx: Any
    _dlg: Any
    _progress: _TtsTestProgressBox | None

    def __init__(self, ctx: Any, dialog: Any) -> None:
        self._ctx = ctx
        self._dlg = dialog
        self._progress = None

    def on_action_performed(self, rEvent: Any) -> None:
        del rEvent
        try:
            self._speak_sample()
        except Exception:
            log.exception("TTS test voice failed")
            self._status(_("Could not play the voice sample."))

    def _status(self, message: str) -> None:
        """Tell the user why the sample did not play. A failed box must not close Settings."""
        log.info("TTS test: %s", message)
        try:
            msgbox(self._ctx, _("Speech"), message)
        except Exception:
            log.debug("TTS test status dialog failed", exc_info=True)

    def _show_progress(self, message: str) -> None:
        if self._progress is None or self._progress._closed:
            self._progress = _TtsTestProgressBox(self._ctx)
        self._progress.show_or_update(message)

    def _close_progress(self) -> None:
        box = self._progress
        self._progress = None
        if box is not None:
            box.close()

    def _control_text(self, *names: str) -> str:
        for name in names:
            ctrl = get_optional(self._dlg, name)
            if ctrl is None or not hasattr(ctrl, "getText"):
                continue
            try:
                return str(ctrl.getText() or "")
            except Exception:
                log.debug("TTS test: could not read %s", name, exc_info=True)
        return ""

    def _speak_sample(self) -> None:
        from plugin.audio.tts_service import speak_text_async
        from plugin.audio.tts_voices import (
            clean_provider_name,
            parse_tts_speed,
            tts_test_sample,
            voice_choice_to_id,
            voice_options_for_provider,
        )

        enabled_ctrl = get_optional(self._dlg, "audio__tts_enabled")
        if enabled_ctrl is not None and is_checkbox_control(enabled_ctrl):
            enabled = get_checkbox_state(enabled_ctrl) == 1
        else:
            enabled = bool(get_config("audio.tts_enabled"))
        if not enabled:
            self._status(
                _("Speech output is off. Turn on Enable Speech Output (TTS) to hear this voice.")
            )
            return

        raw_prov = self._control_text("audio__tts_provider")
        raw_model = self._control_text("audio__tts_model", "tts_model")
        raw_voice = self._control_text("audio__tts_voice")
        raw_speed = self._control_text("audio__tts_speed")
        sample = tts_test_sample()
        options = voice_options_for_provider(
            raw_prov,
            raw_model,
            endpoint=_dialog_endpoint_url(self._dlg),
            api_key=_dialog_api_key(self._dlg),
        )
        voice = voice_choice_to_id(raw_voice, options) if raw_voice.strip() else ""
        # Kokoro's Misaki-vs-espeak choice runs inside speak on this sample.
        log.info(
            "TTS test: provider=%s model=%s voice=%s",
            clean_provider_name(raw_prov) if raw_prov else "",
            raw_model,
            voice,
        )

        # Progress downloads use a modeless box that clear() auto-closes.
        # Sticky errors (HTTP body, fallback) still use MessageBox via __call__.
        on_status = _TtsTestStatusSink(self)

        from plugin.framework.queue_executor import post_to_main_thread

        def _on_complete() -> None:
            post_to_main_thread(self._close_progress)

        speak_text_async(
            sample,
            on_status=on_status,
            on_complete=_on_complete,
            provider=raw_prov or None,
            model=raw_model or None,
            voice=voice or None,
            speed=parse_tts_speed(raw_speed) if raw_speed.strip() else None,
            enabled=True,
        )


class ApiKeyTextListener(BaseListener, XTextListener):
    _el: Any

    def __init__(self, endpoint_listener: Any) -> None:
        self._el = endpoint_listener
    def textChanged(self, rEvent: TextEvent) -> None:
        if not getattr(self._el, "_syncing_api_key", False):
            self._el._user_edited_key = True
        self._el._schedule_debounced_models_fetch()


class EndpointCombinedListener(BaseListener, XItemListener, XTextListener):
    _dlg: Any
    _ctx: Any
    _ctrl: Any
    _debounce_gen: int
    _closed: bool
    _timer: threading.Timer | None
    _synced_endpoint: str | None
    _applied_catalog: tuple[str, str] | None
    _painted_provider: str | None
    _has_painted: bool
    post_to_main_thread: Callable[..., Any]
    run_in_background: Callable[..., Any]
    get_api_key_for_endpoint: Callable[..., Any]
    populate_combobox_with_lru: Callable[..., Any]
    populate_image_model_selector: Callable[..., Any]
    endpoint_from_selector_text: Callable[..., Any]
    endpoint_url_suitable_for_v1_models_fetch: Callable[..., Any]
    fetch_available_models: Callable[..., Any]
    fetch_available_image_models: Callable[..., Any]
    fetch_available_tts_models: Callable[..., Any]
    fetch_available_stt_models: Callable[..., Any]
    settings_catalog_is_warm: Callable[..., Any]
    clear_settings_catalog_cache: Callable[..., Any]
    cached_text_models: Callable[..., Any]
    cached_image_models: Callable[..., Any]
    cached_tts_models: Callable[..., Any]
    cached_stt_models: Callable[..., Any]
    _sanitize_model_combobox_value: Callable[..., Any]
    get_provider_from_endpoint: Callable[..., Any]
    get_image_model: Callable[..., Any]
    get_tts_model: Callable[..., Any]

    def __init__(self, dialog: Any, context: Any, combo_ctrl: Any) -> None:
        from plugin.framework.queue_executor import post_to_main_thread
        from plugin.framework.worker_pool import run_in_background
        from plugin.framework.config import get_api_key_for_endpoint
        from plugin.chatbot.config_ui_helpers import (
            populate_combobox_with_lru, populate_image_model_selector, endpoint_from_selector_text,
            _sanitize_model_combobox_value,
        )
        from plugin.framework.client.provider_detection import get_provider_from_endpoint
        from plugin.framework.client.model_fetcher import (
            cached_image_models, cached_stt_models, cached_text_models, cached_tts_models,
            clear_settings_catalog_cache, endpoint_url_suitable_for_v1_models_fetch,
            fetch_available_models, fetch_available_image_models,
            fetch_available_stt_models, fetch_available_tts_models,
            get_image_model, settings_catalog_is_warm,
        )

        self._dlg = dialog
        self._ctx = context
        self._ctrl = combo_ctrl
        self._debounce_gen = 0
        self._closed = False
        self._timer = None
        # Endpoint + live key last painted from the in-memory catalog.
        # A later keystroke with the same pair must not refetch or rewrite.
        self._applied_catalog = None
        # Provider the model combos were last filled for. None until the
        # first fill; that fill compares against the saved endpoint.
        self._painted_provider = None
        self._has_painted = False

        self.post_to_main_thread = post_to_main_thread
        self.run_in_background = run_in_background
        self.get_api_key_for_endpoint = get_api_key_for_endpoint
        self.populate_combobox_with_lru = populate_combobox_with_lru
        self.populate_image_model_selector = populate_image_model_selector
        self.endpoint_from_selector_text = endpoint_from_selector_text
        self.endpoint_url_suitable_for_v1_models_fetch = endpoint_url_suitable_for_v1_models_fetch
        self.fetch_available_models = fetch_available_models
        self.fetch_available_image_models = fetch_available_image_models
        self.fetch_available_tts_models = fetch_available_tts_models
        self.fetch_available_stt_models = fetch_available_stt_models
        self.settings_catalog_is_warm = settings_catalog_is_warm
        self.clear_settings_catalog_cache = clear_settings_catalog_cache
        self.cached_text_models = cached_text_models
        self.cached_image_models = cached_image_models
        self.cached_tts_models = cached_tts_models
        self.cached_stt_models = cached_stt_models
        self._sanitize_model_combobox_value = _sanitize_model_combobox_value
        self.get_provider_from_endpoint = get_provider_from_endpoint
        self.get_image_model = get_image_model
        self.get_tts_model = get_tts_model
        # URL whose saved key the field was last aligned to. Set from the
        # combo at attach time so the first keystroke is a change, not a baseline.
        try:
            opened = str(combo_ctrl.getText() or "") if combo_ctrl is not None else ""
        except Exception:
            opened = ""
        self._synced_endpoint = self.endpoint_from_selector_text(opened) or None
        # True once the user types in the key field after the last URL sync.
        self._user_edited_key: bool = False
        # Set while _sync_api_key writes the field, so that write is not an edit.
        self._syncing_api_key: bool = False

        self._update_key_link_state()

    def _update_key_link_state(self) -> None:
        # The Get API Key link is gone. A new endpoint still leaves the last
        # connection-test line on screen, so clear that label here.
        lbl_status = get_optional(self._dlg, "lbl_test_status")
        if lbl_status:
            set_control_text(lbl_status, "")

    def _live_api_key(self) -> str:
        ak_ctrl = get_optional(self._dlg, "api_key")
        return str(get_control_text(ak_ctrl)) if ak_ctrl else ""

    def _api_key_override(self) -> str | None:
        """Live key field, or None when that control is absent.

        None matches ``fetch_available_models``: the saved key is hashed into
        the cache id. A present field, including empty, is its own cache id.
        """
        ak_ctrl = get_optional(self._dlg, "api_key")
        if not ak_ctrl:
            return None

        from plugin.chatbot.settings_dialog import effective_api_key

        typed_key = str(get_control_text(ak_ctrl))
        saved_endpoint = get_current_endpoint()
        target_endpoint = self.endpoint_from_selector_text(self._ctrl.getText())
        saved_key = self.get_api_key_for_endpoint(saved_endpoint)
        target_key = self.get_api_key_for_endpoint(target_endpoint)

        return effective_api_key(
            typed_key,
            saved_endpoint,
            target_endpoint,
            saved_key,
            target_key,
            self._user_edited_key,
        )

    def _catalog_is_warm(self, resolved: str) -> bool:
        return bool(self.settings_catalog_is_warm(resolved, api_key_override=self._api_key_override()))

    def _catalog_identity(self, resolved: str) -> tuple[str, str]:
        """Endpoint plus the live key. None (no field) and "" are different slots."""
        override = self._api_key_override()
        return (resolved, "" if override is None else override)

    def _apply_from_cache(self, resolved: str) -> None:
        """Fill combos from the process memo. No HTTP."""
        self._applied_catalog = self._catalog_identity(resolved)
        models = self.cached_text_models(resolved, api_key_override=self._api_key_override())
        self._apply_dropdowns(resolved, models=models, skip_fetch=True)

    def _combo_current_for_provider(self, ctrl: Any, *, same_provider: bool, fallback: str = "") -> str:
        """Return combobox current only when the saved provider still matches.

        After a provider switch the field still holds the previous provider's
        model id (e.g. OpenRouter ``inception/mercury-2.5`` on Together). That
        slug is often missing from ``DEFAULT_MODELS``, so
        ``_is_incompatible_model_for_provider`` will not drop it — discard
        leftover text and let populate use this provider's LRU/defaults.
        """
        if not same_provider:
            return ""
        current = self._sanitize_model_combobox_value(str(ctrl.getText() or ""))
        return current or fallback

    def _apply_dropdowns(self, resolved: str, models: Any = None, skip_fetch: bool = False) -> None:
        api_key_ov = self._live_api_key()
        skip_remote = bool(skip_fetch)
        resolved_provider = self.get_provider_from_endpoint(resolved)
        # The first fill compares against the saved endpoint's provider.
        # Later fills compare against the provider this listener last filled
        # for (_painted_provider). Comparing against the saved endpoint on
        # every fill discards a typed Text model and jumps Image to the
        # catalog's first id after a provider switch that is not OK'd yet.
        # Combos hold ids from the wrong provider only right after the
        # provider changes. Once filled for Together, a Together repaint
        # keeps what the user typed or picked; switching back to the saved
        # provider still drops the Together ids.
        if self._has_painted:
            baseline_provider = self._painted_provider
        else:
            baseline_provider = self.get_provider_from_endpoint(get_current_endpoint())
        same_provider = bool(resolved_provider and resolved_provider == baseline_provider)
        self._painted_provider = resolved_provider
        self._has_painted = True

        text_ctrl = get_optional(self._dlg, "text_model")
        if text_ctrl:
            current = self._combo_current_for_provider(
                text_ctrl,
                same_provider=same_provider,
                fallback=str(get_text_model() or ""),
            )
            self.populate_combobox_with_lru(
                self._ctx,
                text_ctrl,
                current,
                "model_lru",
                resolved,
                remote_models=models,
                api_key_override=api_key_ov,
                skip_remote_fetch=skip_remote,
            )

        stt_ctrl = get_optional(self._dlg, "audio__stt_model") or get_optional(self._dlg, "stt_model")
        if stt_ctrl:
            stt_val = self._combo_current_for_provider(
                stt_ctrl,
                same_provider=same_provider,
                # get_stt_model dual-reads audio.stt_model then legacy stt_model.
                fallback=str(get_stt_model() or ""),
            )
            stt_remote = self._speech_remote_models(resolved, resolved_provider, models, api_key_ov, "stt")
            self.populate_combobox_with_lru(
                self._ctx,
                stt_ctrl,
                stt_val,
                "audio_model_lru",
                resolved,
                remote_models=stt_remote,
                api_key_override=api_key_ov,
                skip_remote_fetch=skip_remote,
            )

        tts_ctrl = get_optional(self._dlg, "audio__tts_model") or get_optional(self._dlg, "tts_model")
        if tts_ctrl:
            tts_val = self._combo_current_for_provider(
                tts_ctrl,
                same_provider=same_provider,
                fallback=str(get_config("audio.tts_model") or self.get_tts_model() or ""),
            )
            tts_remote = self._speech_remote_models(resolved, resolved_provider, models, api_key_ov, "tts")
            self.populate_combobox_with_lru(
                self._ctx,
                tts_ctrl,
                tts_val,
                "tts_model_lru",
                resolved,
                remote_models=tts_remote,
                api_key_override=api_key_ov,
                skip_remote_fetch=skip_remote,
            )
            # Speech-list or Together /v1/voices just filled supported_voices.
            # The model text may be unchanged, so the voice listener would not run.
            TtsSettingsListener(self._dlg, self._ctx).sync_ui()

        image_ctrl = get_optional(self._dlg, "image_model")
        if image_ctrl:
            # The worker stores image ids before this runs. Calling
            # fetch_available_image_models here GETs on the UI thread for any
            # host that is not already in that memo (Ollama used to).
            if models is not None:
                image_models = self.cached_image_models(resolved, api_key_override=self._api_key_override())
            else:
                image_models = None
            image_val = self._combo_current_for_provider(
                image_ctrl,
                same_provider=same_provider,
                fallback=str(self.get_image_model() or ""),
            )
            self.populate_combobox_with_lru(
                self._ctx,
                image_ctrl,
                image_val,
                "image_model_lru",
                resolved,
                remote_models=image_models,
                api_key_override=api_key_ov,
                skip_remote_fetch=skip_remote,
            )

    def _speech_remote_models(
        self,
        resolved: str,
        resolved_provider: str | None,
        models: Any,
        api_key_ov: str,
        kind: str,
    ) -> Any:
        """Ids for the Speech-tab STT or TTS combo.

        OpenRouter's unfiltered ``/v1/models`` list is not a speech catalog, and
        ``output_modalities=audio`` is music (Lyria / gpt-audio), not TTS.
        Together has no modality filter. The combo is the documented serverless
        audio catalog (every ``tts`` / ``default_tts`` or ``stt`` / ``default_audio``
        row), plus remote ids that share those families when ``/v1/models``
        happens to list them.
        """
        if resolved_provider == "together":
            from plugin.framework.default_models import together_speech_ids

            remote = models if isinstance(models, list) else None
            # No key and no fetched catalog yet: same placeholder as the text combo.
            if not api_key_ov and remote is None:
                return None
            return together_speech_ids("tts" if kind == "tts" else "stt", remote)
        if resolved_provider != "openrouter":
            return models
        # The modality GET runs in _bg_fetch. A cache miss stays empty rather
        # than blocking the UI thread on speech or transcription.
        if not isinstance(models, list):
            return None
        fetch = self.cached_tts_models if kind == "tts" else self.cached_stt_models
        found = fetch(resolved, api_key_override=self._api_key_override())
        return found if isinstance(found, list) else None

    def close(self) -> None:
        self._closed = True
        self._debounce_gen += 1
        if self._timer:
            self._timer.cancel()

    def _api_key_field_follows_saved(self, ak_ctrl: Any, previous: str | None) -> bool:
        """True when the field is empty or still shows the saved key for *previous*."""
        current = str(get_control_text(ak_ctrl) or "")
        if not current.strip():
            return True
        if not previous:
            return False
        return current == str(self.get_api_key_for_endpoint(previous) or "")

    def _sync_api_key(self, *, force: bool = False) -> None:
        """Load the saved key when the resolved endpoint changes.

        Rewrite the field only when the resolved URL changes, and only
        if it is empty or still holds the previous URL's saved key. Writing
        get_api_key_for_endpoint on every keystroke wipes a pasted key when
        one character of the URL changes, and OK then stores the restored
        value. Preset clicks pass force=True and always load that preset's
        saved key.
        """
        resolved = self.endpoint_from_selector_text(self._ctrl.getText())
        self._update_key_link_state()
        if not resolved:
            return
        previous = self._synced_endpoint
        if not force and previous == resolved:
            return
        ak_ctrl = get_optional(self._dlg, "api_key")
        if ak_ctrl is not None and (force or self._api_key_field_follows_saved(ak_ctrl, previous)):
            self._syncing_api_key = True
            try:
                set_control_text(ak_ctrl, self.get_api_key_for_endpoint(resolved))
            finally:
                self._syncing_api_key = False
            self._user_edited_key = False
        self._synced_endpoint = resolved

    def _tts_model_id_for_voice_fetch(self) -> str:
        """Speech-model id captured on the UI thread for the voices GET.

        The worker must not read the combo. ``_run_fetch`` and Test Connection
        call this before ``run_in_background``.
        """
        ctrl = get_optional(self._dlg, "audio__tts_model") or get_optional(self._dlg, "tts_model")
        if ctrl is None or not hasattr(ctrl, "getText"):
            return ""
        try:
            return str(ctrl.getText() or "").strip()
        except Exception:
            log.debug("TTS model id for voice fetch unavailable", exc_info=True)
            return ""

    def _bg_fetch(self, gen: int, resolved: str, tts_model_id: str = "", key_ov: str | None = None) -> None:
        if self._closed or gen != self._debounce_gen: return

        models = None
        if resolved and self.endpoint_url_suitable_for_v1_models_fetch(resolved):
            models = self.fetch_available_models(resolved, api_key_override=key_ov)
        provider = self.get_provider_from_endpoint(resolved) if resolved else None
        # These GETs used to run in _apply_dropdowns on the UI thread after the
        # text list returned. OpenRouter image is GET /v1/images/models; speech
        # and transcription are separate modality queries; Together voices are
        # GET /v1/voices. That blocked Settings, including OK. The worker fills
        # the process caches; apply_ui only reads them.
        if resolved and provider in {"openrouter", "together"}:
            self.fetch_available_image_models(resolved, api_key_override=key_ov)
            if provider == "openrouter":
                self.fetch_available_tts_models(resolved, api_key_override=key_ov)
                self.fetch_available_stt_models(resolved, api_key_override=key_ov)
            else:
                from plugin.framework.client.model_fetcher import fetch_together_tts_voices

                # Ask for the model captured on the UI thread, then list-all
                # so the other Speech rows and the warm check stay filled.
                # A list-all memo does not fill the ?model= entry the combo
                # asked for, and sync_ui must not GET /v1/voices on the UI
                # thread. apply_ui only reads cached_tts_supported_voices.
                if tts_model_id:
                    fetch_together_tts_voices(
                        resolved, model_id=tts_model_id, api_key_override=key_ov,
                    )
                fetch_together_tts_voices(resolved, api_key_override=key_ov)

        def apply_ui() -> None:
            if self._closed or gen != self._debounce_gen: return
            if self.endpoint_from_selector_text(self._ctrl.getText()) != resolved: return
            self._applied_catalog = self._catalog_identity(resolved)
            self._apply_dropdowns(resolved, models=models, skip_fetch=(models is None))

        self.post_to_main_thread(apply_ui)

    def _schedule_debounced_models_fetch(self) -> None:
        resolved = self.endpoint_from_selector_text(self._ctrl.getText())
        # Once per process for this endpoint+key. A warm memo fills the combos
        # and must not start a timer or a worker. The same pair already painted
        # is not painted again: typing must not rewrite fields. Test Connection
        # is the recheck.
        if resolved and self._catalog_is_warm(resolved):
            if self._timer:
                self._timer.cancel()
            self._debounce_gen += 1
            if self._catalog_identity(resolved) == self._applied_catalog:
                return
            self._apply_from_cache(resolved)
            return
        if self._timer: self._timer.cancel()
        self._debounce_gen += 1
        gen = self._debounce_gen
        self._timer = threading.Timer(1.0, lambda: self.post_to_main_thread(lambda: self._run_fetch(gen)))
        self._timer.daemon = True
        self._timer.start()

    def _run_fetch(self, gen: int) -> None:
        # close() bumps the generation and cancels the timer. A callback already
        # posted to the UI thread must not start the worker.
        if self._closed or gen != self._debounce_gen:
            return
        resolved = self.endpoint_from_selector_text(self._ctrl.getText())
        if resolved:
            tts_model_id = self._tts_model_id_for_voice_fetch()
            key_ov = self._api_key_override()
            self.run_in_background(
                lambda: self._bg_fetch(gen, resolved, tts_model_id, key_ov), name="settings-fetch",
            )

    def force_catalog_refresh(self) -> None:
        """Clear this endpoint+key memo and refetch off the UI thread."""
        resolved = self.endpoint_from_selector_text(self._ctrl.getText())
        if not resolved:
            return
        self.clear_settings_catalog_cache(resolved, api_key_override=self._api_key_override())
        if self._timer:
            self._timer.cancel()
        self._debounce_gen += 1
        gen = self._debounce_gen
        tts_model_id = self._tts_model_id_for_voice_fetch()
        self.run_in_background(
            lambda: self._bg_fetch(gen, resolved, tts_model_id), name="settings-recheck",
        )

    def textChanged(self, rEvent: TextEvent) -> None:
        # A typed URL reloads that URL's saved key, but only while the field
        # still follows the saved key (_api_key_field_follows_saved). A key the
        # user typed stays. Without this, the debounced fetch sent the previous
        # endpoint's key to the newly typed URL. A warm catalog is not refetched.
        del rEvent
        self._sync_api_key()
        self._schedule_debounced_models_fetch()

    def itemStateChanged(self, rEvent: ItemEvent) -> None:
        idx = getattr(rEvent, "Selected", -1)
        if idx < 0: return
        item = self._ctrl.getItem(idx)
        if not item: return
        
        url = self.endpoint_from_selector_text(item)
        if url: self._ctrl.setText(url)
        
        if self._timer: self._timer.cancel()
        self._debounce_gen += 1
        # Snapshot on the UI thread. The worker used to read _debounce_gen
        # when it started, so a newer click was invisible, and it omitted the
        # Together TTS model id that force_catalog_refresh already captures.
        gen = self._debounce_gen
        resolved = self.endpoint_from_selector_text(self._ctrl.getText())
        if resolved:
            self._sync_api_key(force=True)
            if self._catalog_is_warm(resolved):
                self._apply_from_cache(resolved)
                return
            # LRU plus defaults now. The catalog GET is the worker below.
            # Non-OpenRouter providers used to fetch inside _apply_dropdowns
            # and froze the dialog on a dead host.
            self._apply_dropdowns(resolved, models=None, skip_fetch=True)
            tts_model_id = self._tts_model_id_for_voice_fetch()
            self.run_in_background(
                lambda: self._bg_fetch(gen, resolved, tts_model_id), name="settings-select",
            )


# ── Evaluation Dashboard ─────────────────────────────────────────────



# ── Helper for module tabs ───────────────────────────────────────────

def setup_module_tabs(dlg: Any) -> None:
    """Register action listeners for module-specific tabs in the Settings dialog."""
    try:
        from plugin._manifest import MODULES
        from plugin.chatbot.settings_tab_order import iter_settings_tab_modules

        # Map button ID to step index (starting from 3 for module tabs)
        # Core tabs: 1=Chat, 2=Image
        step = 3
        for m in iter_settings_tab_modules(cast("list[dict[str, Any]]", MODULES)):
            m_name = str(m.get("name", ""))
            prefix = m_name.replace(".", "_")
            btn_id = f"btn_tab_{prefix}"
            btn = get_optional(dlg, btn_id)
            if btn:
                btn.addActionListener(TabListener(dlg, step))
                step += 1
    except ImportError:
        pass
    except Exception:
        log.exception("Failed to setup module tabs")


class DownloadAudioListener(BaseActionListener):
    """Settings → Python: download audio binaries and pure Python dependencies from GitHub."""

    _ctx: Any
    _dlg: Any

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.scripting.native_binaries import run_audio_download

        def probe(on_display: Callable[[str], None], on_status: Callable[[str], None]) -> tuple[bool, str]:
            ok = run_audio_download(on_display, on_status)
            return ok, ""

        VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg).run_modal_probe(
            probe, title=_("Audio Library Download")
        )


# ── Evaluation Dashboard ─────────────────────────────────────────────

from plugin.chatbot.eval_dashboard_ui import (
    EvalDashboard,
    EvalRunListener,
    SimpleCloseListener,
    show_eval_dashboard,
)


# ── MCP UI Integration ───────────────────────────────────────────────

from plugin.mcp.mcp_ui import (
    CopyMcpConfigListener,
    McpPortTextListener,
    McpTunnelEnabledListener,
    McpTunnelProviderListener,
    TestTunnelListener,
    _PROVIDER_DEFAULT_URLS,
    _tested_provider_tunnel_urls,
    build_mcp_config_snippet,
    clear_active_settings_dialog,
    notify_tunnel_url_acquired,
    set_active_settings_dialog,
    sync_mcp_config_snippet,
)

__all__ = [
    "CopyMcpConfigListener",
    "DownloadAudioListener",
    "EndpointCombinedListener",
    "EvalDashboard",
    "EvalRunListener",
    "McpPortTextListener",
    "McpTunnelEnabledListener",
    "McpTunnelProviderListener",
    "ProviderStarterListener",
    "SettingsDialog",
    "SimpleCloseListener",
    "TestConnectionListener",
    "TestTunnelListener",
    "_PROVIDER_DEFAULT_URLS",
    "_tested_provider_tunnel_urls",
    "build_mcp_config_snippet",
    "clear_active_settings_dialog",
    "input_box",
    "notify_tunnel_url_acquired",
    "open_system_url",
    "set_active_settings_dialog",
    "settings_box",
    "setup_module_tabs",
    "show_eval_dashboard",
    "sync_mcp_config_snippet",
]



