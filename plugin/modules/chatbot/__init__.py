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
"""AI chat sidebar module."""

import logging

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("writeragent.chatbot")


class ChatbotModule(ModuleBase):
    """Registers the chatbot sidebar and its tool adapter."""

    def initialize(self, services):
        self._services = services
        self._routes_registered = False
        self._api_handler = None

        from . import web_research
        services.tools.auto_discover(web_research)

        # Chat tool routing is now handled natively by main.py's get_tools() instead of ChatToolAdapter
        self._adapter = None

        # Always register API routes (legacy Chat API) when http_routes is available.
        # The old chatbot.api_enabled toggle was removed from the manifest, so the
        # routes are now unconditionally enabled for the HTTP server.
        self._register_routes(services)

    def _register_routes(self, services):
        routes = services.get("http_routes")
        if not routes:
            log.warning("http_routes service not available")
            return

        try:
            from plugin.modules.chatbot.handler import ChatApiHandler
            self._api_handler = ChatApiHandler(services)
            routes.add("POST", "/api/chat",
                       self._api_handler.handle_chat, raw=True)
            routes.add("GET", "/api/chat",
                       self._api_handler.handle_history)
            routes.add("DELETE", "/api/chat",
                       self._api_handler.handle_reset)
            routes.add("GET", "/api/providers",
                       self._api_handler.handle_providers)
            self._routes_registered = True
            log.info("Chat API routes registered")
        except Exception as exc:  # ImportError, AttributeError, or route add failure
            log.info(
                "Chat API handler not available; skipping /api/chat routes: %s",
                exc,
            )
            self._api_handler = None

    def _unregister_routes(self, services):
        routes = services.get("http_routes")
        if routes:
            for method, path in [
                ("POST", "/api/chat"),
                ("GET", "/api/chat"),
                ("DELETE", "/api/chat"),
                ("GET", "/api/providers"),
            ]:
                try:
                    routes.remove(method, path)
                except Exception:
                    pass
        self._routes_registered = False
        log.info("Chat API routes unregistered")

    def get_adapter(self):
        """Return the ChatToolAdapter for use by the panel factory."""
        return self._adapter

    # ── Action dispatch ──────────────────────────────────────────────

    def on_action(self, action):
        if action == "extend_selection":
            self._action_extend_selection()
        elif action == "edit_selection":
            self._action_edit_selection()
        else:
            super().on_action(action)

    # ── Extend Selection ─────────────────────────────────────────────

    def _action_extend_selection(self):
        """Get document selection -> stream AI completion -> append to text."""
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.dialogs import msgbox

        ctx = get_ctx()
        doc_svc = self._services.document
        doc = doc_svc.get_active_document()
        if not doc:
            msgbox(ctx, "WriterAgent", "No document open")
            return

        doc_type = doc_svc.detect_doc_type(doc)
        if doc_type == "writer":
            self._extend_writer(ctx, doc)
        elif doc_type == "calc":
            self._extend_calc(ctx, doc)
        else:
            msgbox(ctx, "WriterAgent",
                   "Extend selection not supported for this document type")

    def _extend_writer(self, ctx, doc):
        """Extend selection in a Writer document."""
        from plugin.framework.dialogs import msgbox
        from plugin.framework.async_stream import run_stream_async
        from plugin.framework.config import get_api_config
        from plugin.modules.http.client import LlmClient

        try:
            selection = doc.CurrentController.getSelection()
            text_range = selection.getByIndex(0)
            selected_text = text_range.getString()
        except Exception:
            msgbox(ctx, "WriterAgent", "No text selected")
            return

        if not selected_text:
            msgbox(ctx, "WriterAgent", "No text selected")
            return

        config = self._services.config.proxy_for("chatbot")
        system_prompt = config.get("system_prompt") or ""
        _mt = config.get("extend_selection_max_tokens") or 70
        max_tokens = int(float(_mt))

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": selected_text})

        def apply_chunk(text, is_thinking=False):
            if not is_thinking:
                try:
                    text_range.setString(text_range.getString() + text)
                except Exception:
                    log.exception("Failed to append text")

        def on_error(e):
            log.error("Extend selection failed: %s", e)
            msgbox(ctx, "WriterAgent: Extend Selection", str(e))

        api_config = get_api_config(ctx)
        client = LlmClient(api_config, ctx)
        run_stream_async(
            ctx, client, messages, tools=None,
            apply_chunk_fn=apply_chunk,
            on_done_fn=lambda: None,
            on_error_fn=on_error,
            max_tokens=max_tokens,
        )

    def _extend_calc(self, ctx, doc):
        """Extend selection in a Calc document."""
        from plugin.framework.dialogs import msgbox
        from plugin.framework.async_stream import run_stream_async
        from plugin.framework.config import get_api_config
        from plugin.modules.http.client import LlmClient

        try:
            sheet = doc.CurrentController.ActiveSheet
            selection = doc.CurrentController.Selection
            area = selection.getRangeAddress()
        except Exception:
            msgbox(ctx, "WriterAgent", "No cells selected")
            return

        config = self._services.config.proxy_for("chatbot")
        system_prompt = config.get("system_prompt") or ""
        _mt = config.get("extend_selection_max_tokens") or 70
        max_tokens = int(float(_mt))

        # Build task list
        tasks = []
        cell_range = sheet.getCellRangeByPosition(area.StartColumn, area.StartRow, area.EndColumn, area.EndRow)
        data_array = cell_range.getDataArray()

        for row_idx, row in enumerate(range(area.StartRow, area.EndRow + 1)):
            for col_idx, col in enumerate(range(area.StartColumn, area.EndColumn + 1)):
                raw_val = data_array[row_idx][col_idx]
                cell_text = str(raw_val) if raw_val != "" and raw_val is not None else ""

                if cell_text:
                    cell = sheet.getCellByPosition(col, row)
                    tasks.append((cell, cell_text))

        if not tasks:
            msgbox(ctx, "WriterAgent", "No cells with content selected")
            return

        api_config = get_api_config(ctx)
        client = LlmClient(api_config, ctx)

        # Process cells sequentially via callback chain
        task_index = [0]

        def run_next_cell():
            if task_index[0] >= len(tasks):
                return
            cell, cell_text = tasks[task_index[0]]
            task_index[0] += 1

            msgs = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            msgs.append({"role": "user", "content": cell_text})

            def apply_chunk(text, is_thinking=False):
                if not is_thinking:
                    try:
                        cell.setString(cell.getString() + text)
                    except Exception:
                        pass

            def on_error(e):
                log.error("Extend selection (calc) failed: %s", e)
                msgbox(ctx, "WriterAgent: Extend Selection", str(e))

            run_stream_async(
                ctx, client, msgs, tools=None,
                apply_chunk_fn=apply_chunk,
                on_done_fn=run_next_cell,
                on_error_fn=on_error,
                max_tokens=max_tokens,
            )

        run_next_cell()

    # ── Edit Selection ───────────────────────────────────────────────

    def _action_edit_selection(self):
        """Get selection -> input instructions -> stream AI -> replace text."""
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.dialogs import msgbox

        ctx = get_ctx()
        doc_svc = self._services.document
        doc = doc_svc.get_active_document()
        if not doc:
            msgbox(ctx, "WriterAgent", "No document open")
            return

        doc_type = doc_svc.detect_doc_type(doc)
        if doc_type == "writer":
            self._edit_writer(ctx, doc)
        elif doc_type == "calc":
            self._edit_calc(ctx, doc)
        else:
            msgbox(ctx, "WriterAgent",
                   "Edit selection not supported for this document type")

    def _show_edit_input(self):
        """Show the edit instructions dialog. Returns (user_input, extra_instructions); empty strings if cancelled.
        Uses the shared EditInputDialog.xdl (legacy_ui.input_box) so menu and shortcut share the same UI.
        """
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.legacy_ui import input_box
        ctx = get_ctx()
        user_input, extra_instructions = input_box(
            ctx, "Please enter edit instructions!", "Input", ""
        )
        return user_input, extra_instructions

    def _edit_writer(self, ctx, doc):
        """Edit selection in a Writer document."""
        from plugin.framework.dialogs import msgbox
        from plugin.framework.async_stream import run_stream_async
        from plugin.framework.config import get_api_config
        from plugin.modules.http.client import LlmClient

        try:
            selection = doc.CurrentController.getSelection()
            text_range = selection.getByIndex(0)
            original_text = text_range.getString()
        except Exception:
            msgbox(ctx, "WriterAgent", "No text selected")
            return

        if not original_text:
            msgbox(ctx, "WriterAgent", "No text selected")
            return

        user_input, extra_instructions = self._show_edit_input()
        if not user_input:
            return
        if extra_instructions:
            from plugin.framework.config import set_config, update_lru_history, get_current_endpoint
            set_config(ctx, "additional_instructions", extra_instructions)
            update_lru_history(ctx, extra_instructions, "prompt_lru", get_current_endpoint(ctx))

        config = self._services.config.proxy_for("chatbot")
        system_prompt = extra_instructions or config.get("system_prompt") or ""
        _mnt = config.get("edit_selection_max_new_tokens") or 0
        max_new_tokens = int(float(_mnt))

        prompt = (
            "ORIGINAL VERSION:\n" + original_text +
            "\n Below is an edited version according to the following "
            "instructions. There are no comments in the edited version. "
            "The edited version is followed by the end of the document. "
            "The original version will be edited as follows to create "
            "the edited version:\n" + user_input + "\nEDITED VERSION:\n"
        )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        max_tokens = len(original_text) + max_new_tokens

        # Clear selection and start streaming replacement
        text_range.setString("")

        def apply_chunk(text, is_thinking=False):
            if not is_thinking:
                try:
                    text_range.setString(text_range.getString() + text)
                except Exception:
                    log.exception("Failed to write text")

        def on_error(e):
            try:
                text_range.setString(original_text)
            except Exception:
                pass
            log.error("Edit selection failed: %s", e)
            msgbox(ctx, "WriterAgent: Edit Selection", str(e))

        api_config = get_api_config(ctx)
        client = LlmClient(api_config, ctx)
        run_stream_async(
            ctx, client, messages, tools=None,
            apply_chunk_fn=apply_chunk,
            on_done_fn=lambda: None,
            on_error_fn=on_error,
            max_tokens=max_tokens,
        )

    def _edit_calc(self, ctx, doc):
        """Edit selection in a Calc document."""
        from plugin.framework.dialogs import msgbox
        from plugin.framework.async_stream import run_stream_async
        from plugin.framework.config import get_api_config
        from plugin.modules.http.client import LlmClient

        try:
            sheet = doc.CurrentController.ActiveSheet
            selection = doc.CurrentController.Selection
            area = selection.getRangeAddress()
        except Exception:
            msgbox(ctx, "WriterAgent", "No cells selected")
            return

        user_input, extra_instructions = self._show_edit_input()
        if not user_input:
            return
        if extra_instructions:
            from plugin.framework.config import set_config, update_lru_history, get_current_endpoint
            set_config(ctx, "additional_instructions", extra_instructions)
            update_lru_history(ctx, extra_instructions, "prompt_lru", get_current_endpoint(ctx))

        config = self._services.config.proxy_for("chatbot")
        system_prompt = extra_instructions or config.get("system_prompt") or ""
        _mnt = config.get("edit_selection_max_new_tokens") or 0
        max_new_tokens = int(float(_mnt))

        # Build task list
        tasks = []
        cell_range = sheet.getCellRangeByPosition(area.StartColumn, area.StartRow, area.EndColumn, area.EndRow)
        data_array = cell_range.getDataArray()

        for row_idx, row in enumerate(range(area.StartRow, area.EndRow + 1)):
            for col_idx, col in enumerate(range(area.StartColumn, area.EndColumn + 1)):
                raw_val = data_array[row_idx][col_idx]
                original = str(raw_val) if raw_val != "" and raw_val is not None else ""

                prompt = (
                    "ORIGINAL VERSION:\n" + original +
                    "\n Below is an edited version according to the following "
                    "instructions. Don't waste time thinking, be as fast as "
                    "you can. The edited text will be a shorter or longer "
                    "version of the original text based on the instructions. "
                    "There are no comments in the edited version. The edited "
                    "version is followed by the end of the document. The "
                    "original version will be edited as follows to create "
                    "the edited version:\n" + user_input +
                    "\nEDITED VERSION:\n"
                )
                max_tokens = len(original) + max_new_tokens

                cell = sheet.getCellByPosition(col, row)
                tasks.append((cell, prompt, max_tokens, original))

        if not tasks:
            return

        api_config = get_api_config(ctx)
        client = LlmClient(api_config, ctx)

        # Process cells sequentially
        task_index = [0]

        def run_next_cell():
            if task_index[0] >= len(tasks):
                return
            cell, prompt, max_tok, original = tasks[task_index[0]]
            task_index[0] += 1

            cell.setString("")

            msgs = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            msgs.append({"role": "user", "content": prompt})

            def apply_chunk(text, is_thinking=False):
                if not is_thinking:
                    try:
                        cell.setString(cell.getString() + text)
                    except Exception:
                        pass

            def on_error(e):
                try:
                    cell.setString(original)
                except Exception:
                    pass
                log.error("Edit selection (calc) failed: %s", e)
                msgbox(ctx, "WriterAgent: Edit Selection", str(e))

            run_stream_async(
                ctx, client, msgs, tools=None,
                apply_chunk_fn=apply_chunk,
                on_done_fn=run_next_cell,
                on_error_fn=on_error,
                max_tokens=max_tok,
            )

        run_next_cell()
